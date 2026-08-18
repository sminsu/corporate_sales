"""Semantic schema loading, prompt context builders, and schema validation."""

import functools
import json
import os
import re
from datetime import datetime
from decimal import Decimal

import sqlparse
import yaml

from .config import DB_SCHEMA, DB_SCHEMA_PREFIX, SCHEMA_PATH
from .db import _sql_identifier_view, _validate_read_only_sql
from .llm import _call_llm, _normalize_llm_text
from .time_policy import format_accumulation_policy, kst_today, previous_day_ymd
from .tools.verified_queries import load_external_verified_queries
from .v2.column_synonyms import phrase_in_text as _v2_phrase_in_text
from .v2.vq_output_guard import vq_output_gap as _v2_vq_output_gap


def _memoize_by_schema_identity(func):
    """Cache a ``schema -> str`` summary builder keyed on the schema object identity.

    The summary builders are pure functions of the (effectively immutable) loaded
    schema, yet they are called on every graph node. Memoizing avoids rebuilding the
    same large strings on each request. The cache holds a reference to the schema
    object so its ``id`` cannot be reused by a different object while cached.
    """
    cache: dict[int, tuple[object, str]] = {}

    @functools.wraps(func)
    def wrapper(schema: dict, *args, **kwargs) -> str:
        # Question/domain-scoped summaries must not accumulate an unbounded
        # per-question cache.  Keep the fast identity cache only for the
        # backwards-compatible, schema-only call form.
        if args or kwargs:
            return func(schema, *args, **kwargs)
        key = id(schema)
        hit = cache.get(key)
        if hit is not None and hit[0] is schema:
            return hit[1]
        result = func(schema)
        cache[key] = (schema, result)
        return result

    return wrapper

# ---------------------------------------------------------------------------
# 3. Semantic Layer 로더
# ---------------------------------------------------------------------------

_SENSITIVE_COLUMN_RE = re.compile(
    r"(?:주민등록번호|고객고유번호|계좌번호|카드번호|이메일|전자주소|"
    r"전화번호|휴대전화번호|휴대폰번호|상세주소|여권번호|운전면허번호|외국인등록번호)"
)


def _is_semantic_table_visible(table: dict) -> bool:
    return str(table.get("semantic_visibility") or "default").lower() != "restricted"


def _is_restricted_column(table: dict, column_name: object) -> bool:
    name = str(column_name or "")
    restricted = {str(value).lower() for value in table.get("restricted_columns", [])}
    return name.lower() in restricted or bool(_SENSITIVE_COLUMN_RE.search(name))


def _visible_columns(table: dict, section: str) -> list[dict]:
    return [
        column
        for column in table.get(section, [])
        if not _is_restricted_column(table, column.get("name"))
    ]


def _rewrite_schema_prefix(node):
    """로드된 스키마 트리의 모든 문자열에서 'card_system.' prefix를 설정값으로 치환한다.

    YAML 원본은 card_system. 으로 고정돼 있지만, 실제 실행 대상(PostgreSQL schema 또는
    Athena Glue database)의 이름이 다를 수 있다. 메모리에 올린 스키마(physical_table,
    verified query sql, sql_pattern 등)를 일괄 치환해 프롬프트·검증·실행을 일관시킨다.
    """
    if isinstance(node, dict):
        return {key: _rewrite_schema_prefix(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_rewrite_schema_prefix(item) for item in node]
    if isinstance(node, str) and "card_system." in node:
        return node.replace("card_system.", DB_SCHEMA_PREFIX)
    return node


def _with_contract_compatibility(schema: dict) -> dict:
    """Expose legacy contract keys in memory while YAML uses one compact source.

    Older callers and quality checks still read ``llm_semantic_contract`` and
    ``time_resolution_rules`` directly.  Deriving those aliases at load time
    preserves that public surface without reintroducing duplicate YAML rules.
    Prompt builders continue to prefer ``sql_generation_contract``.
    """
    contract = schema.get("sql_generation_contract")
    if not isinstance(contract, dict) or not contract:
        return schema
    compatible = dict(schema)
    compatible.setdefault(
        "llm_semantic_contract",
        {
            "purpose": contract.get("purpose", "Athena SQL 생성 계약"),
            "sql_output_rules": [
                *contract.get("athena_rules", []),
                *contract.get("grain_and_aggregation_rules", []),
            ],
            "evidence_priority": contract.get("evidence_order", []),
            "ambiguity_policy": contract.get("ambiguity_rules", []),
        },
    )
    compatible.setdefault(
        "time_resolution_rules",
        [
            {"phrase": phrase, "resolve_to": resolution}
            for phrase, resolution in (contract.get("time_resolution") or {}).items()
        ],
    )
    compatible.setdefault("result_shape_defaults", contract.get("result_defaults", {}))
    return compatible


def load_semantic_layer(path: str | None = None) -> dict:
    if path is None:
        path = os.getenv("SEMANTIC_SCHEMA_PATH", str(SCHEMA_PATH))
    with open(path, "r", encoding="utf-8") as f:
        schema = yaml.safe_load(f)
    # 기본 스키마(card_system)와 다를 때만 치환 비용을 들인다.
    if DB_SCHEMA != "card_system":
        schema = _rewrite_schema_prefix(schema)
    return _with_contract_compatibility(schema)


@_memoize_by_schema_identity
def build_table_summary(schema: dict) -> str:
    lines = []
    for t in schema.get("tables", []):
        if not _is_semantic_table_visible(t):
            continue
        name = t["name"]
        desc = t.get("description", "").strip().replace("\n", " ")
        desc = _SENSITIVE_COLUMN_RE.sub("[민감정보]", desc)
        for restricted_name in t.get("restricted_columns", []):
            desc = desc.replace(str(restricted_name), "[제한컬럼]")
        grain = t.get("grain", "")
        lines.append(f"- **{name}** ({t.get('physical_table', name)}): {desc} [grain: {grain}]")
        accumulation_summary = format_accumulation_policy(t.get("accumulation_policy"))
        if accumulation_summary:
            lines.append(f"  accumulation_policy: {accumulation_summary}")
        dims = _visible_columns(t, "dimensions")
        if dims:
            dim_names = [d["name"] for d in dims[:8]]
            lines.append(f"  dimensions: {', '.join(dim_names)}" + (" ..." if len(dims) > 8 else ""))
        measures = _visible_columns(t, "measures")
        if measures:
            m_names = [m["name"] for m in measures[:6]]
            lines.append(f"  measures: {', '.join(m_names)}" + (" ..." if len(measures) > 6 else ""))
        tds = _visible_columns(t, "time_dimensions")
        if tds:
            lines.append(f"  time_dimensions: {', '.join(td['name'] for td in tds)}")
        partition = t.get("athena_partition")
        if isinstance(partition, dict) and partition.get("enabled") is not False:
            keys = partition.get("keys") or []
            key_names = [key if isinstance(key, str) else key.get("name", "") for key in keys]
            key_names = [name for name in key_names if name]
            if key_names:
                source_time = partition.get("source_time_dimension", "")
                source_note = f" from {source_time}" if source_time else ""
                lines.append(f"  athena_partition: {', '.join(key_names)}{source_note}")
        filters = t.get("filters", [])
        if filters:
            f_strs = [fl["name"] + ": " + fl["expr"] for fl in filters]
            lines.append(f"  filters: {'; '.join(f_strs)}")
        lines.append("")
    return "\n".join(lines)


@_memoize_by_schema_identity
def build_metrics_summary(
    schema: dict,
    question: str = "",
    domain_name: str = "",
    max_count: int = 8,
) -> str:
    """Return a bounded, canonical metric context.

    ``metrics`` was a stale duplicate of ``canonical_metrics`` and sometimes
    exposed a different formula for the same business term.  Canonical metrics
    are now the only runtime source; callers may narrow them by question and/or
    routed domain while the schema-only call remains backwards compatible.
    """
    metrics = list(schema.get("canonical_metrics", []))
    if domain_name:
        domain = _domain_by_name(schema).get(domain_name, {})
        preferred = {str(name) for name in domain.get("preferred_metrics", [])}
        metrics = [
            metric
            for metric in metrics
            if metric.get("domain") == domain_name or str(metric.get("name") or "") in preferred
        ]

    if question:
        scored = []
        q_compact = _compact_text(question)
        for position, metric in enumerate(metrics):
            terms = [metric.get("name", ""), *metric.get("synonyms", [])]
            exact = max(
                (
                    len(compact)
                    for term in terms
                    if (compact := _compact_text(term)) and len(compact) >= 2 and compact in q_compact
                ),
                default=0,
            )
            overlap, specific = _retrieval_overlap(
                question,
                _join_texts(
                    metric.get("name"),
                    metric.get("synonyms", []),
                    metric.get("description"),
                    metric.get("business_definition"),
                    metric.get("source_table"),
                ),
            )
            if exact or overlap >= 2 or specific:
                scored.append((exact, overlap, -position, metric))
        scored.sort(reverse=True, key=lambda item: item[:3])
        metrics = [metric for _, _, _, metric in scored]

    metrics = metrics[: max(0, max_count)]
    if not metrics:
        return "(질문과 직접 관련된 canonical metric 없음)"

    lines = []
    for metric in metrics:
        name = str(metric.get("name") or "")
        definition = str(
            metric.get("business_definition")
            or metric.get("description")
            or "공식 지표"
        ).strip()
        expression = str(metric.get("expression") or "").strip()
        source = str(metric.get("source_table") or "")
        unit = str(metric.get("unit") or "")
        time_dimension = str(metric.get("default_time_dimension") or "")
        lines.append(f"- **{name}**: {definition}")
        if expression:
            lines.append(f"  expression: {expression}")
        metadata = [value for value in (f"table={source}" if source else "", f"time={time_dimension}" if time_dimension else "", f"unit={unit}" if unit else "") if value]
        if metadata:
            lines.append("  " + ", ".join(metadata))
        if metric.get("required_filters"):
            lines.append("  required_filters: " + "; ".join(str(value) for value in metric.get("required_filters", [])))
        if metric.get("aggregation_behavior"):
            lines.append(f"  aggregation_behavior: {metric.get('aggregation_behavior')}")
        if metric.get("synonyms"):
            lines.append("  synonyms: [" + ", ".join(str(value) for value in metric.get("synonyms", [])) + "]")
        for key, label in (
            ("result_grain", "result_grain"),
            ("window_expression", "window_expression"),
            ("numerator_expression", "numerator_expression"),
            ("denominator_expression", "denominator_expression"),
            ("time_policy", "time_policy"),
            ("name_filter", "name_filter"),
            ("semantic_cautions", "semantic_cautions"),
            ("support_status", "support_status"),
        ):
            value = metric.get(key)
            if value in (None, "", [], {}):
                continue
            rendered = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
            lines.append(f"  {label}: {rendered}")
    return "\n".join(lines)


@_memoize_by_schema_identity
def build_glossary_summary(
    schema: dict,
    question: str = "",
    domain_name: str = "",
    max_count: int = 6,
) -> str:
    entries = []
    for position, item in enumerate(schema.get("glossary", [])):
        domains = item.get("domains", item.get("domain", []))
        if isinstance(domains, str):
            domains = [domains]
        if domain_name and domains and domain_name not in domains:
            continue
        if not question:
            entries.append((0, 0, -position, item))
            continue
        terms = [item.get("term", ""), *item.get("aliases", [])]
        q_compact = _compact_text(question)
        exact = max(
            (
                len(compact)
                for term in terms
                if (compact := _compact_text(term)) and len(compact) >= 2 and compact in q_compact
            ),
            default=0,
        )
        overlap, specific = _retrieval_overlap(
            question,
            _join_texts(
                item.get("term"),
                item.get("aliases", []),
                item.get("canonical"),
                item.get("description"),
                item.get("sql_hint"),
            ),
        )
        if exact or overlap >= 2 or specific:
            entries.append((exact, overlap, -position, item))
    entries.sort(reverse=True, key=lambda value: value[:3])

    lines = []
    for _, _, _, g in entries[: max(0, max_count)]:
        aliases = ", ".join(g.get("aliases", []))
        lines.append(f"- **{g['term']}** ({g['canonical']}): {g['description']}")
        if aliases:
            lines.append(f"  aliases: [{aliases}]")
        if g.get("sql_hint"):
            lines.append(f"  sql_hint: {g['sql_hint']}")
    return "\n".join(lines) if lines else "(질문과 직접 관련된 용어 없음)"


_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣_]+")


def _tokenize_text(text: str) -> set[str]:
    tokens = set()
    for token in _TOKEN_RE.findall((text or "").lower()):
        if len(token) >= 2:
            tokens.add(token)
    return tokens


_GENERIC_RETRIEVAL_TOKENS = {
    "가맹점", "거래", "고객", "기준", "기업", "금액", "기간", "매출", "분석", "보여줘",
    "법인", "사업체", "업체", "회사",
    "과거", "당시", "사용", "상태", "시점별", "알려줘", "연도별", "월별", "이용", "일별",
    "전체", "조회", "지금", "최근", "최신", "특정", "현재", "현시점", "현황", "회원", "카드",
    "목록", "명단", "리스트", "찾아줘",
    "이용금액", "사용금액", "매출금액", "카드이용금액", "카드사용금액",
}


def _compact_text(value: object) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣_]", "", str(value or "").lower())


_HISTORICAL_PERIOD_RE = re.compile(
    r"(?:"
    r"20\d{2}\s*년|(?<!\d)20\d{4}(?:\d{2})?(?!\d)|20\d{2}[./-]\d{1,2}|"
    r"(?<!\d)\d{2}(?:0[1-9]|1[0-2])\s*(?:기준|월)(?!\d)|\d{1,2}\s*월|"
    r"(?:최근|지난)\s*(?:\d+|일|반)\s*(?:개월|달|년)|"
    r"당시|과거|월별|일별|연도별|시점별|기간별|분기|상반기|하반기|"
    r"어제|내일|지난|전월|전년|작년|올해"
    r")"
)

_SOURCE_POLICY_EXPLICIT_PERIOD_RE = re.compile(
    r"(?:"
    r"20\d{2}\s*년|(?<!\d)20\d{4}(?:\d{2})?(?!\d)|20\d{2}[./-]\d{1,2}|"
    r"(?<!\d)\d{2}(?:0[1-9]|1[0-2])\s*(?:기준|월)(?!\d)|\d{1,2}\s*월|"
    r"분기|상반기|하반기|작년|지난해|전년|당시|저번\s*달|지난\s*달|전월"
    r")"
)


_EXPLICIT_DAY_RE = re.compile(
    r"(?<!\d)(20\d{2})[-./]?([01]\d)[-./]?([0-3]\d)(?!\d)|"
    r"(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일"
)


def _explicit_day_values(question: str) -> set[str]:
    values: set[str] = set()
    for match in _EXPLICIT_DAY_RE.finditer(question or ""):
        year, month, day = match.groups()[:3] if match.group(1) else match.groups()[3:]
        value = f"{year}{int(month):02d}{int(day):02d}"
        try:
            datetime.strptime(value, "%Y%m%d")
        except ValueError:
            continue
        values.add(value)
    return values


def _explicit_previous_day_is_live_snapshot(question: str) -> bool:
    """Treat a lone explicit KST D-1 date as the available live snapshot."""
    if _explicit_day_values(question) != {previous_day_ymd(kst_today())}:
        return False
    remaining = _EXPLICIT_DAY_RE.sub(" ", question or "")
    return not _SOURCE_POLICY_EXPLICIT_PERIOD_RE.search(remaining)


def _explicit_period_is_live_snapshot(question: str) -> bool:
    """Treat an explicitly written current month or KST D-1 as live state."""
    if _explicit_previous_day_is_live_snapshot(question):
        return True
    if not re.search(r"현재|현재\s*기준|오늘|전일|어제|최신|지금", question or ""):
        return False
    current_ym = kst_today().strftime("%Y%m")
    explicit_months = {
        f"{match.group(1)}{int(match.group(2)):02d}"
        for match in re.finditer(r"(20\d{2})\s*년\s*(\d{1,2})\s*월", question or "")
        if 1 <= int(match.group(2)) <= 12
    }
    explicit_months.update(
        re.findall(r"(?<!\d)(20\d{2}(?:0[1-9]|1[0-2]))(?!\d)", question or "")
    )
    return bool(explicit_months) and explicit_months == {current_ym}


def _has_historical_period_expression(question: str) -> bool:
    """Whether a question explicitly asks for a historical/as-of attribute."""
    text = question or ""
    if _explicit_previous_day_is_live_snapshot(text):
        text = _EXPLICIT_DAY_RE.sub(" ", text)
    return bool(_HISTORICAL_PERIOD_RE.search(text))


def source_tables_for_question(entry: dict, question: str) -> list[str]:
    """Resolve a declarative current/monthly source policy for one question."""
    policy = entry.get("source_table_policy")
    fallback = entry.get("source_tables")
    if fallback is None:
        fallback = [entry.get("source_table")] if entry.get("source_table") else []
    if not isinstance(policy, dict):
        return [str(value) for value in fallback or [] if value]

    current_terms = policy.get("current_snapshot_when") or []
    current_snapshot = any(
        _phrase_in_text(question, term) for term in current_terms
    )
    # A concrete as-of month wins even when Korean uses "현재" to mean
    # "as of that month" (for example, "2026년 4월 현재").  Generic
    # "과거 이용 + 현재 상태", however, is a mixed live/history request.
    explicit_period = bool(_SOURCE_POLICY_EXPLICIT_PERIOD_RE.search(question or ""))
    if _explicit_period_is_live_snapshot(question):
        explicit_period = False
        current_snapshot = True
    if not current_snapshot and re.search(r"과거", question or ""):
        explicit_period = True
    key = "period_snapshot" if explicit_period else "current_snapshot" if current_snapshot else "default"
    selected = policy.get(key) or policy.get("default") or fallback or []
    if isinstance(selected, str):
        selected = [selected]
    return [str(value) for value in selected if value]


def _phrase_in_text(question: str, phrase: object) -> bool:
    """Match a Korean business phrase without crossing adjacent word boundaries.

    v2: 구분자와 중첩 조사에 둔감한 매처로 교체했다. 이전 구현은
      - phrase 에 적힌 토큰 사이에서만 구분자를 허용해서 '기업카드신용한도금액' 이
        "기업카드 신용한도금액" 을 놓쳤고,
      - 조사를 한 개만 허용해서 "회원자격별로"(별+로)를 놓쳤다.
    이 매처는 _rule_rank_tables() 의 테이블 선택과 _table_details() 의 컬럼 선택에
    함께 쓰이므로, 여기가 프롬프트에 어떤 컬럼이 들어가는지를 결정한다.
    """
    return _v2_phrase_in_text(question, phrase)


def semantic_attribute_candidates(
    schema: dict,
    question: str,
    domain_name: str = "",
    max_count: int = 6,
) -> list[dict]:
    """Return reusable semantic attributes relevant to the question.

    Attributes model business roles such as merchant owner, corporate customer,
    or payment institution independently from any one query template.  This
    keeps physical-column selection and code-value resolution in the semantic
    layer while returning only a small prompt context for local models.
    """
    scored: list[tuple[int, int, int, dict]] = []
    for position, attribute in enumerate(schema.get("semantic_attributes", [])):
        domains = attribute.get("domains", attribute.get("domain", []))
        if isinstance(domains, str):
            domains = [domains]
        if domain_name and domains and domain_name not in domains:
            continue
        terms = [
            attribute.get("name", ""),
            attribute.get("korean_name", ""),
            *attribute.get("aliases", []),
        ]
        exact = max(
            (
                len(compact)
                for term in terms
                if (compact := _compact_text(term))
                and len(compact) >= 2
                and _phrase_in_text(question, term)
            ),
            default=0,
        )
        overlap, _specific = _retrieval_overlap(
            question,
            _join_texts(
                terms,
                attribute.get("business_definition"),
                attribute.get("source_mappings", []),
                attribute.get("value_semantics", {}),
                attribute.get("semantic_cautions", []),
            ),
        )
        value_hit = 0
        for raw_value in (attribute.get("value_semantics") or {}).values():
            value_info = raw_value if isinstance(raw_value, dict) else {"label": raw_value}
            value_terms = [
                value_info.get("label", ""),
                *value_info.get("aliases", []),
            ]
            value_hit = max(
                value_hit,
                max(
                    (
                        len(compact)
                        for term in value_terms
                        if (compact := _compact_text(term))
                        and len(compact) >= 2
                        and _phrase_in_text(question, term)
                    ),
                    default=0,
                ),
            )
        # Attribute routing requires an explicit attribute phrase or a full
        # code label.  Free-text overlap with definitions/cautions is too broad
        # for selecting physical codebook tables.
        if exact or value_hit:
            scored.append((max(exact, value_hit), overlap, -position, attribute))
    scored.sort(key=lambda item: item[:3], reverse=True)
    return [attribute for _, _, _, attribute in scored[: max(0, max_count)]]


def resolve_semantic_attribute_value(
    schema: dict,
    attribute_or_parameter: str,
    question: str,
) -> str:
    """Resolve a code value from a semantic attribute's labels and aliases."""
    q_compact = _compact_text(question)
    matches: list[tuple[int, str]] = []

    def occurrences(term: str) -> list[tuple[int, int]]:
        spans = []
        start = 0
        while term and (index := q_compact.find(term, start)) >= 0:
            spans.append((index, index + len(term)))
            start = index + 1
        return spans

    def semantic_value_terms(attribute: dict) -> set[str]:
        return {
            compact
            for raw_value in (attribute.get("value_semantics") or {}).values()
            for value_info in [raw_value if isinstance(raw_value, dict) else {"label": raw_value}]
            for term in [value_info.get("label", ""), *value_info.get("aliases", [])]
            if (compact := _compact_text(term))
        }

    def attribute_cues(attribute: dict) -> set[str]:
        value_terms = semantic_value_terms(attribute)
        return {
            compact
            for term in [
                attribute.get("name", ""),
                attribute.get("korean_name", ""),
                attribute.get("parameter_name", ""),
                *attribute.get("aliases", []),
            ]
            if (compact := _compact_text(term)) and compact not in value_terms
        }

    for attribute in schema.get("semantic_attributes", []):
        names = {
            str(attribute.get("name") or ""),
            str(attribute.get("parameter_name") or ""),
        }
        if attribute_or_parameter not in names:
            continue
        target_cue_hit = any(cue in q_compact for cue in attribute_cues(attribute))
        foreign_attributes = [
            other for other in schema.get("semantic_attributes", []) if other is not attribute
        ]
        foreign_terms = {
            term for other in foreign_attributes for term in semantic_value_terms(other)
        }
        for code, raw_value in (attribute.get("value_semantics") or {}).items():
            value_info = raw_value if isinstance(raw_value, dict) else {"label": raw_value}
            terms = [value_info.get("label", ""), *value_info.get("aliases", [])]
            for term in terms:
                compact = _compact_text(term)
                if len(compact) < 2:
                    continue
                # The same label can be a valid value in multiple business
                # codebooks (for example 주부, 지점, 은행, 기타).  A bare
                # duplicate is ambiguous, so resolve it only when the question
                # names this attribute explicitly.
                if compact in foreign_terms and not target_cue_hit:
                    continue
                target_spans = occurrences(compact)
                shadow_spans = [
                    span
                    for foreign in foreign_terms
                    if len(foreign) > len(compact) and compact in foreign
                    for span in occurrences(foreign)
                ]
                if any(
                    not any(start <= target_start and target_end <= end for start, end in shadow_spans)
                    for target_start, target_end in target_spans
                ):
                    matches.append((len(compact), str(code)))
    return max(matches)[1] if matches else ""


def build_semantic_attributes_summary(
    schema: dict,
    question: str = "",
    domain_name: str = "",
    max_count: int = 6,
) -> str:
    """Render a bounded question-specific semantic attribute context."""
    if question:
        attributes = semantic_attribute_candidates(
            schema,
            question,
            domain_name=domain_name,
            max_count=max_count,
        )
        index = {
            str(attribute.get("name") or ""): attribute
            for attribute in schema.get("semantic_attributes", [])
        }
        bound = []
        for contract in semantic_query_contract_candidates(
            schema,
            question,
            domain_name=domain_name,
            max_count=2,
        ):
            for name in contract.get("semantic_attributes", []):
                attribute = index.get(str(name))
                if attribute and attribute not in bound:
                    bound.append(attribute)
        attributes = (bound + [item for item in attributes if item not in bound])[: max(0, max_count)]
    else:
        attributes = list(schema.get("semantic_attributes", []))[: max(0, max_count)]
    if not attributes:
        return "(질문과 직접 관련된 semantic attribute 없음)"

    lines = []
    for attribute in attributes:
        name = attribute.get("korean_name") or attribute.get("name") or ""
        lines.append(f"- **{name}**: {attribute.get('business_definition', '')}")
        if attribute.get("aliases"):
            lines.append(
                "  aliases: [" + ", ".join(str(value) for value in attribute.get("aliases", [])) + "]"
            )
        if attribute.get("parameter_name"):
            lines.append(f"  parameter_name: {attribute.get('parameter_name')}")
        if attribute.get("codebook_status"):
            lines.append(f"  codebook_status: {attribute.get('codebook_status')}")
        if attribute.get("value_semantics_provenance"):
            lines.append(
                "  value_semantics_provenance: "
                + str(attribute.get("value_semantics_provenance"))
            )
        if attribute.get("codebook_validity"):
            lines.append(
                "  codebook_validity: "
                + json.dumps(attribute.get("codebook_validity"), ensure_ascii=False)
            )
        if attribute.get("source_selection"):
            lines.append(
                "  source_selection: "
                + json.dumps(attribute.get("source_selection"), ensure_ascii=False)
            )
        mappings = attribute.get("source_mappings", [])
        if mappings:
            lines.append("  source_mappings:")
            for mapping in mappings[:8]:
                table = mapping.get("table", "")
                columns = mapping.get("columns", mapping.get("column", []))
                if isinstance(columns, str):
                    columns = [columns]
                role = mapping.get("role", "")
                role_note = f", role={role}" if role else ""
                lines.append(f"    - {table}: {', '.join(str(value) for value in columns)}{role_note}")
        if attribute.get("value_semantics"):
            lines.append(
                "  value_semantics: "
                + json.dumps(attribute.get("value_semantics"), ensure_ascii=False)
            )
        if attribute.get("filter_expression"):
            lines.append(f"  filter_expression: {attribute.get('filter_expression')}")
        if attribute.get("semantic_cautions"):
            lines.append(
                "  semantic_cautions: "
                + "; ".join(str(value) for value in attribute.get("semantic_cautions", []))
            )
    return "\n".join(lines)


def _retrieval_overlap(question: str, evidence: str) -> tuple[int, bool]:
    """Return semantic token overlap and whether it contains a specific hit.

    One generic token such as ``기업`` or ``카드`` must not retrieve a long,
    unrelated SQL example.  A single non-generic business term remains useful
    for concise requests such as ``연체 알려줘``.
    """
    evidence_lower = str(evidence or "").lower()
    generic_suffixes = {
        "의", "은", "는", "이", "가", "을", "를", "에", "에서", "에게",
        "로", "으로", "와", "과", "도", "만", "별", "중", "인",
    }
    question_tokens = set()
    for token in _tokenize_text(question):
        normalized = token
        for generic in sorted(_GENERIC_RETRIEVAL_TOKENS, key=len, reverse=True):
            if token.startswith(generic) and token[len(generic) :] in generic_suffixes:
                normalized = generic
                break
        question_tokens.add(normalized)
    matched = {token for token in question_tokens if token in evidence_lower}
    specific = any(token not in _GENERIC_RETRIEVAL_TOKENS and len(token) >= 2 for token in matched)
    return len(matched), specific


def _normalized_table_name(value: object) -> str:
    return str(value or "").strip().rsplit(".", 1)[-1].lower()


def _domain_table_names(schema: dict, domain_name: str) -> set[str]:
    if not domain_name:
        return set()
    domains = {str(item.get("name") or ""): item for item in schema.get("canonical_domains", [])}
    domain = domains.get(domain_name, {})
    entity_index = {str(item.get("name") or ""): item for item in schema.get("semantic_entities", [])}
    names = {_normalized_table_name(domain.get("default_fact_table"))}
    for entity_name in domain.get("primary_entities", []):
        names.add(_normalized_table_name(entity_index.get(str(entity_name), {}).get("physical_table")))
    preferred = {str(name) for name in domain.get("preferred_metrics", [])}
    for metric in schema.get("canonical_metrics", []):
        if metric.get("domain") == domain_name or str(metric.get("name") or "") in preferred:
            names.add(_normalized_table_name(metric.get("source_table")))
    return {name for name in names if name}


def _entry_matches_domain(
    schema: dict,
    entry: dict,
    domain_name: str,
    table_names: set[str],
) -> bool:
    if not domain_name:
        return True
    domains = entry.get("domains", entry.get("domain", []))
    if isinstance(domains, str):
        domains = [domains]
    if domains:
        return domain_name in domains
    domain_tables = _domain_table_names(schema, domain_name)
    return not table_names or bool(domain_tables.intersection(table_names))


def _score_by_question(question: str, *texts: object) -> int:
    q_tokens = _tokenize_text(question)
    if not q_tokens:
        return 0
    joined = " ".join(str(text or "") for text in texts).lower()
    score = 0
    for token in q_tokens:
        if token in joined:
            score += 1
    return score


def resolve_query_reference_for_question(ref: dict, question: str) -> dict:
    """Render only the cadence sources applicable to this reference request."""
    policy = ref.get("source_table_policy")
    if not isinstance(policy, dict):
        return ref

    governed = {
        _normalized_table_name(table)
        for key in ("default", "current_snapshot", "period_snapshot")
        for table in (policy.get(key) or [])
        if table
    }
    selected = source_tables_for_question(ref, question)
    primary = str(ref.get("primary_table") or "")
    joins = [str(table) for table in ref.get("join_tables", []) if table]
    unrelated_joins = [
        table for table in joins if _normalized_table_name(table) not in governed
    ]

    resolved = dict(ref)
    if _normalized_table_name(primary) in governed and selected:
        resolved["primary_table"] = selected[0]
        resolved["join_tables"] = list(
            dict.fromkeys([*selected[1:], *unrelated_joins])
        )
    else:
        resolved["join_tables"] = list(dict.fromkeys([*selected, *unrelated_joins]))
    return resolved


def _format_query_reference(ref: dict, question: str = "") -> str:
    ref = resolve_query_reference_for_question(ref, question)
    lines = [f"- **{ref.get('intent', '')}**"]
    says = ", ".join(ref.get("when_user_says", [])[:3])
    if says:
        lines.append(f"  user_says: [{says}]")
    if ref.get("primary_table"):
        lines.append(f"  primary_table: {ref['primary_table']}")
    if ref.get("join_tables"):
        lines.append(f"  join_tables: {', '.join(ref.get('join_tables', []))}")
    if ref.get("join_rule"):
        lines.append(f"  join_rule: {ref['join_rule']}")
    if ref.get("verified_query"):
        lines.append(f"  verified_query: {ref['verified_query']}")
    if ref.get("source_table_policy"):
        lines.append(
            "  source_table_policy: "
            + json.dumps(ref.get("source_table_policy"), ensure_ascii=False)
        )
    if ref.get("source_lineage"):
        lines.append("  source_lineage: " + json.dumps(ref.get("source_lineage"), ensure_ascii=False))
    cols = ref.get("recommended_columns", {})
    if cols:
        lines.append("  column_hints:")
        for key, value in list(cols.items())[:6]:
            lines.append(f"    - {key}: {value}")
    required_parameters = ref.get("required_parameters", [])
    if required_parameters:
        if isinstance(required_parameters, dict):
            required_parameters = list(required_parameters)
        lines.append("  required_parameters: " + ", ".join(str(value) for value in required_parameters))
    rules = ref.get("rules", [])
    if rules:
        lines.append("  rules:")
        for rule in rules[:4]:
            lines.append(f"    - {rule}")
    if ref.get("sql_pattern"):
        lines.append(f"  sql_pattern:\n{ref['sql_pattern'].strip()}")
    return "\n".join(lines)


def _semantic_query_contract_score(schema: dict, question: str, contract: dict) -> int:
    """Score a compositional semantic contract using declarative phrase groups."""
    compact = _compact_text(question)
    match = contract.get("match") if isinstance(contract.get("match"), dict) else {}

    def phrase_hit(value: object) -> bool:
        normalized = _compact_text(value)
        return bool(normalized and len(normalized) >= 2 and normalized in compact)

    excluded = match.get("excluded", [])
    if any(phrase_hit(value) for value in excluded):
        return 0
    if match.get("exclude_explicit_period") and _has_historical_period_expression(question):
        return 0
    if match.get("require_explicit_period") and not _has_historical_period_expression(question):
        return 0

    required = match.get("required", [])
    if not required:
        return 0
    score = 0
    for group in required:
        phrases = group if isinstance(group, list) else [group]
        hits = [value for value in phrases if phrase_hit(value)]
        if not hits:
            return 0
        score += 12 + max(len(_compact_text(value)) for value in hits)

    for group in match.get("optional", []):
        phrases = group if isinstance(group, list) else [group]
        hits = [value for value in phrases if phrase_hit(value)]
        if hits:
            score += 3 + max(len(_compact_text(value)) for value in hits)

    for attribute_name in match.get("required_attribute_values", []):
        if not resolve_semantic_attribute_value(schema, str(attribute_name), question):
            return 0
        score += 18

    for example in contract.get("examples", []):
        normalized = _compact_text(example)
        if len(normalized) >= 4 and normalized in compact:
            score += 30 + len(normalized)
    return score


def semantic_query_contract_candidates(
    schema: dict,
    question: str,
    domain_name: str = "",
    max_count: int = 2,
    *,
    routing_only: bool = False,
) -> list[dict]:
    """Return matched reusable query contracts, strongest first."""
    scored: list[tuple[int, int, dict]] = []
    for position, contract in enumerate(schema.get("semantic_query_contracts", [])):
        domain = str(contract.get("domain") or "")
        if domain_name and domain and domain != domain_name:
            continue
        if routing_only and str(contract.get("routing_strength") or "medium").lower() != "high":
            continue
        score = _semantic_query_contract_score(schema, question, contract)
        if score > 0:
            scored.append((score, -position, contract))
    scored.sort(key=lambda item: item[:2], reverse=True)
    return [contract for _, _, contract in scored[: max(0, max_count)]]


def _format_semantic_query_contract(contract: dict, question: str = "") -> str:
    contract = dict(contract)
    if contract.get("source_table_policy"):
        contract["source_tables"] = source_tables_for_question(contract, question)
    lines = [f"- **{contract.get('name', '')}** (domain={contract.get('domain', '')})"]
    if contract.get("description"):
        lines.append(f"  definition: {contract.get('description')}")
    if contract.get("support_status"):
        lines.append(f"  support_status: {contract.get('support_status')}")
    fields = (
        ("execution_mode", "execution_mode"),
        ("reference_query", "reference_query"),
        ("source_tables", "source_tables"),
        ("source_table_policy", "source_table_policy"),
        ("require_all_selected_tables", "require_all_selected_tables"),
        ("sql_shape", "sql_shape"),
        ("metric_names", "canonical_metrics"),
        ("result_grain", "result_grain"),
        ("dimensions", "dimensions"),
        ("semantic_attributes", "semantic_attributes"),
        ("entity_binding", "entity_binding"),
        ("name_filter", "name_filter"),
        ("time_policy", "time_policy"),
        ("deduplication", "deduplication"),
        ("filters", "filters"),
        ("calculation", "calculation"),
        ("result_fields", "result_fields"),
        ("ambiguity_policy", "ambiguity_policy"),
    )
    for key, label in fields:
        value = contract.get(key)
        if value in (None, "", [], {}):
            continue
        rendered = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
        lines.append(f"  {label}: {rendered}")
    return "\n".join(lines)


def find_relevant_semantic_query_contracts(
    schema: dict,
    question: str,
    domain_name: str = "",
    max_count: int = 2,
) -> str:
    contracts = semantic_query_contract_candidates(
        schema,
        question,
        domain_name=domain_name,
        max_count=max_count,
    )
    if not contracts:
        return "(질문과 직접 관련된 semantic query contract 없음)"
    return "\n\n".join(
        _format_semantic_query_contract(contract, question) for contract in contracts
    )


def find_relevant_references(
    schema: dict,
    question: str,
    max_count: int = 2,
    domain_name: str = "",
) -> str:
    refs = schema.get("query_references", [])
    if not refs:
        return "(관련 reference 없음)"
    scored = []
    q_compact = _compact_text(question)
    for position, ref in enumerate(refs):
        ref_tables = {
            _normalized_table_name(ref.get("primary_table")),
            *(_normalized_table_name(value) for value in ref.get("join_tables", [])),
        }
        ref_tables.discard("")
        if not _entry_matches_domain(schema, ref, domain_name, ref_tables):
            continue
        exact_length = 0
        for phrase in ref.get("when_user_says", []):
            phrase_compact = _compact_text(phrase)
            if len(phrase_compact) >= 4 and phrase_compact in q_compact:
                exact_length = max(exact_length, len(phrase_compact))
        evidence = _join_texts(
            ref.get("intent"),
            ref.get("when_user_says", []),
            ref.get("recommended_columns", {}),
            ref.get("rules", []),
            ref.get("join_rule"),
        )
        overlap, specific = _retrieval_overlap(question, evidence)
        if exact_length or overlap >= 2 or specific:
            scored.append((exact_length, overlap, -position, ref))
    scored.sort(key=lambda item: item[:3], reverse=True)
    # A curated normalized phrase is authoritative and should not be diluted by
    # generic references sharing words such as "기업", "회원", or "카드".
    exact = [ref for exact_length, _, _, ref in scored if exact_length > 0]
    top = exact[:max_count] if exact else [ref for _, _, _, ref in scored[:max_count]]
    if not top:
        return "(질문과 직접 관련된 reference 없음)"
    return "\n\n".join(_format_query_reference(ref, question) for ref in top)


def _sql_table_names(sql: str) -> set[str]:
    return {
        _normalized_table_name(match.group(1))
        for match in re.finditer(
            r'\b(?:FROM|JOIN)\s+(?:(?:[A-Za-z_][A-Za-z0-9_]*|"[^"]+")\.)?"?([A-Za-z_][A-Za-z0-9_]*)"?',
            str(sql or ""),
            flags=re.IGNORECASE,
        )
    }


def find_relevant_queries(
    schema: dict,
    question: str,
    max_count: int = 1,
    domain_name: str = "",
) -> str:
    vqs = schema.get("verified_queries", [])
    scored = []
    q_compact = _compact_text(question)
    for position, vq in enumerate(vqs):
        if str(vq.get("runtime_mode") or "executable").lower() == "reference_only":
            continue
        if not _entry_matches_domain(schema, vq, domain_name, _sql_table_names(vq.get("sql", ""))):
            continue
        vq_compact = _compact_text(vq.get("question"))
        exact_length = len(vq_compact) if len(vq_compact) >= 4 and vq_compact in q_compact else 0
        evidence = _join_texts(
            vq.get("name"),
            vq.get("question"),
            vq.get("description"),
            vq.get("tags", []),
            vq.get("parameters", {}),
        )
        overlap, specific = _retrieval_overlap(question, evidence)
        # Tags are curated intent labels; matching several is stronger than a
        # shared generic word inside a long SQL example.
        tag_hits = sum(
            1
            for tag in vq.get("tags", [])
            if len(_compact_text(tag)) >= 2 and _compact_text(tag) in q_compact
        )
        # v2: 프롬프트에 붙는 참고 SQL은 로컬 모델이 거의 그대로 베낀다. 질문이
        # 요구한 기간·축·지표를 못 내놓는 예시는 붙이지 않는다. 붙여 놓으면
        # "카드등급별 할부연체원금" 질문에 카드등급별 연체율 SQL이 그대로 나온다.
        if _v2_vq_output_gap(question, vq, column_names=_schema_column_names(schema)):
            continue
        if exact_length or overlap >= 2 or tag_hits >= 2 or (specific and tag_hits >= 1):
            scored.append((exact_length, tag_hits, overlap, -position, vq))
    scored.sort(key=lambda item: item[:4], reverse=True)
    exact = [vq for exact_length, _, _, _, vq in scored if exact_length > 0]
    top = exact[:max_count] if exact else [vq for _, _, _, _, vq in scored[:max_count]]
    if not top:
        return "(질문과 직접 관련된 verified query 없음)"
    lines = []
    for vq in top:
        lines.append(f"Q: {vq['question']}")
        lines.append(f"SQL:\n{vq['sql'].strip()}\n")
    return "\n".join(lines)


_COLUMN_NAMES_CACHE_KEY = "_v2_column_names_cache"


def _schema_column_names(schema: dict) -> frozenset[str]:
    """Dimension/measure 컬럼명 전체. VQ 출력 계약 검사에 쓴다.

    캐시는 스키마 딕셔너리 안에 담는다. id(schema) 를 키로 쓰면 임시 스키마가
    GC 된 뒤 같은 id 를 재사용한 다른 스키마에 엉뚱한 값이 붙는다.
    """
    cached = schema.get(_COLUMN_NAMES_CACHE_KEY)
    if cached is None:
        cached = frozenset(
            str(column.get("name") or "")
            for table in schema.get("tables", [])
            for section in ("dimensions", "measures")
            for column in (table.get(section) or [])
            if column.get("name")
        )
        schema[_COLUMN_NAMES_CACHE_KEY] = cached
    return cached


def _join_texts(*values: object) -> str:
    parts = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(_join_texts(item) for item in value)
        elif isinstance(value, dict):
            parts.append(json.dumps(value, ensure_ascii=False))
        else:
            parts.append(str(value))
    return " ".join(part for part in parts if part)


def _domain_by_name(schema: dict) -> dict[str, dict]:
    return {d.get("name", ""): d for d in schema.get("canonical_domains", []) if d.get("name")}


def _entity_by_name(schema: dict) -> dict[str, dict]:
    return {e.get("name", ""): e for e in schema.get("semantic_entities", []) if e.get("name")}


def _canonical_metrics_by_domain(schema: dict) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for metric in schema.get("canonical_metrics", []):
        grouped.setdefault(metric.get("domain", ""), []).append(metric)
    return grouped


@_memoize_by_schema_identity
def build_semantic_contract_summary(schema: dict) -> str:
    preferred_contract = schema.get("sql_generation_contract")
    if isinstance(preferred_contract, dict) and preferred_contract:
        lines: list[str] = []

        def append_value(label: str, value: object, indent: int = 0) -> None:
            prefix = "  " * indent
            readable_label = str(label).replace("_", " ")
            if isinstance(value, dict):
                lines.append(f"{prefix}{readable_label}:")
                for key, nested in value.items():
                    append_value(str(key), nested, indent + 1)
                return
            if isinstance(value, list):
                lines.append(f"{prefix}{readable_label}:")
                for item in value[:12]:
                    if isinstance(item, dict):
                        lines.append(f"{prefix}  - {json.dumps(item, ensure_ascii=False)}")
                    else:
                        lines.append(f"{prefix}  - {item}")
                return
            lines.append(f"{prefix}{readable_label}: {value}")

        for key, value in preferred_contract.items():
            append_value(str(key), value)
        return "\n".join(lines)

    contract = schema.get("llm_semantic_contract", {})
    lines = []
    if contract.get("purpose"):
        lines.append(f"purpose: {contract['purpose'].strip()}")
    section_labels = [
        ("sql_output_rules", "SQL output rules"),
        ("evidence_priority", "Evidence priority"),
        ("ambiguity_policy", "Ambiguity policy"),
    ]
    for key, label in section_labels:
        values = contract.get(key, [])
        if values:
            lines.append(f"{label}:")
            for idx, value in enumerate(values, 1):
                lines.append(f"{idx}. {value}")
    time_rules = schema.get("time_resolution_rules", [])
    if time_rules:
        lines.append("Time resolution rules:")
        for rule in time_rules:
            lines.append(f"- {json.dumps(rule, ensure_ascii=False)}")
    metric_rules = schema.get("metric_generation_rules", [])
    if metric_rules:
        lines.append("Metric generation rules:")
        for rule in metric_rules:
            lines.append(f"- {rule.get('name')}: {rule.get('rule')}")
    result_defaults = schema.get("result_shape_defaults", {})
    if result_defaults:
        lines.append(f"Result shape defaults: {json.dumps(result_defaults, ensure_ascii=False)}")
    return "\n".join(lines) if lines else "(llm_semantic_contract 없음)"


def semantic_join_paths_for_tables(
    schema: dict,
    table_names: list[str] | set[str] | tuple[str, ...] | None = None,
    *,
    require_both: bool = True,
) -> list[dict]:
    """Return canonical safe paths enriched with physical table endpoints.

    Newer paths may declare ``from_table``/``to_table`` directly.  Existing
    paths continue to resolve those values through their semantic entities.
    When table names are supplied, callers can request paths whose endpoints
    are both selected (SQL detail) or either selected (catalog neighbour
    expansion) without consulting the legacy ``relationships`` section.
    """
    entity_index = _entity_by_name(schema)
    selected = {_normalized_table_name(name) for name in (table_names or []) if name}
    results = []
    for raw_path in schema.get("semantic_join_graph", {}).get("safe_paths", []):
        path = dict(raw_path)
        from_table = path.get("from_table") or entity_index.get(path.get("from_entity", ""), {}).get("physical_table", "")
        to_table = path.get("to_table") or entity_index.get(path.get("to_entity", ""), {}).get("physical_table", "")
        from_table = _normalized_table_name(from_table)
        to_table = _normalized_table_name(to_table)
        if not from_table or not to_table:
            continue
        path["from_table"] = from_table
        path["to_table"] = to_table
        if selected:
            endpoints = {from_table, to_table}
            matches = selected.intersection(endpoints)
            if require_both and not endpoints.issubset(selected):
                continue
            if not require_both and not matches:
                continue
        results.append(path)
    return results


def _semantic_join_paths_for_domain(schema: dict, domain_name: str, question: str = "", max_paths: int = 8) -> list[dict]:
    domain = _domain_by_name(schema).get(domain_name, {})
    primary_entities = set(domain.get("primary_entities", []))
    domain_tables = _domain_table_names(schema, domain_name)
    if not primary_entities and not domain_tables:
        return []
    q_tokens = _tokenize_text(question)
    scored = []
    for path in semantic_join_paths_for_tables(schema):
        from_entity = path.get("from_entity")
        to_entity = path.get("to_entity")
        endpoints = {path.get("from_table"), path.get("to_table")}
        touches_domain = (
            from_entity in primary_entities
            or to_entity in primary_entities
            or bool(domain_tables.intersection(endpoints))
        )
        if not touches_domain:
            continue
        score = 2.0
        if (
            from_entity in primary_entities and to_entity in primary_entities
        ) or endpoints.issubset(domain_tables):
            score += 3.0
        use_when_text = _join_texts(path.get("use_when", []), path.get("name", ""), path.get("caution", ""))
        overlap = q_tokens.intersection(_tokenize_text(use_when_text))
        score += len(overlap)
        scored.append((score, path))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in scored[:max_paths]]


def build_semantic_join_context(
    schema: dict,
    domain_name: str,
    question: str = "",
    max_paths: int = 8,
    table_names: list[str] | set[str] | tuple[str, ...] | None = None,
) -> str:
    paths = _semantic_join_paths_for_domain(schema, domain_name, question, max_paths=max_paths)
    if table_names:
        allowed = {
            (path.get("from_table"), path.get("to_table"))
            for path in semantic_join_paths_for_tables(schema, table_names, require_both=True)
        }
        paths = [path for path in paths if (path.get("from_table"), path.get("to_table")) in allowed]
    if not paths:
        return "(선택 도메인에 연결된 semantic_join_graph.safe_paths 없음)"
    lines = ["Use only these safe semantic join paths when they fit the question:"]
    for path in paths:
        from_entity = path.get("from_entity", "")
        to_entity = path.get("to_entity", "")
        from_table = path.get("from_table", "")
        to_table = path.get("to_table", "")
        from_label = from_entity or from_table
        to_label = to_entity or to_table
        lines.append(
            f"- {path.get('name')}: {from_label}({from_table}) -> {to_label}({to_table}), "
            f"type={path.get('join_type')}, join={path.get('sql')}"
        )
        if path.get("use_when"):
            lines.append(f"  use_when: {', '.join(path.get('use_when', []))}")
        if path.get("caution"):
            lines.append(f"  caution: {path.get('caution')}")
    return "\n".join(lines)


def build_domain_context(schema: dict, domain_name: str, max_metrics: int = 8) -> str:
    if not domain_name:
        return "(선택된 도메인 없음)"
    domain = _domain_by_name(schema).get(domain_name)
    if not domain:
        return f"(선택된 도메인 '{domain_name}'이 canonical_domains에 없습니다.)"

    entity_index = _entity_by_name(schema)
    metrics = list(_canonical_metrics_by_domain(schema).get(domain_name, []))
    preferred_names = {str(name) for name in domain.get("preferred_metrics", [])}
    known_metric_names = {str(metric.get("name") or "") for metric in metrics}
    for metric in schema.get("canonical_metrics", []):
        metric_name = str(metric.get("name") or "")
        if metric_name in preferred_names and metric_name not in known_metric_names:
            metrics.append(metric)
            known_metric_names.add(metric_name)
    metrics = metrics[:max_metrics]
    lines = [
        f"- selected_domain: {domain_name}",
        f"- business_scope: {domain.get('business_scope', '')}",
        f"- default_fact_table: {domain.get('default_fact_table', '')}",
        f"- default_time_dimension: {domain.get('default_time_dimension', '')}",
    ]
    if domain.get("default_filters"):
        lines.append(f"- default_filters: {'; '.join(domain.get('default_filters', []))}")
    if domain.get("primary_entities"):
        lines.append("- primary_entities:")
        for entity_name in domain.get("primary_entities", []):
            entity = entity_index.get(entity_name, {})
            physical = entity.get("physical_table", "")
            grain = entity.get("grain", "")
            korean = entity.get("korean_name", entity_name)
            lines.append(f"  - {entity_name} ({korean}, {physical}, grain: {grain})")
    if metrics:
        lines.append("- canonical_metrics:")
        for metric in metrics:
            lines.append(
                f"  - {metric.get('name')}: {metric.get('expression')} "
                f"[table={metric.get('source_table')}, time={metric.get('default_time_dimension')}]"
            )
            if metric.get("required_filters"):
                lines.append(f"    required_filters: {'; '.join(metric.get('required_filters', []))}")
            if metric.get("synonyms"):
                lines.append(f"    synonyms: {', '.join(metric.get('synonyms', []))}")
    return "\n".join(lines)


def _build_domain_embedding_text(schema: dict, domain: dict) -> str:
    domain_name = domain.get("name", "")
    metric_texts = []
    for metric in _canonical_metrics_by_domain(schema).get(domain_name, []):
        metric_texts.append(
            _join_texts(
                metric.get("name"),
                metric.get("synonyms", []),
                metric.get("expression"),
                metric.get("source_entity"),
                metric.get("source_table"),
            )
        )
    entity_index = _entity_by_name(schema)
    entity_texts = []
    for entity_name in domain.get("primary_entities", []):
        entity = entity_index.get(entity_name, {})
        entity_texts.append(
            _join_texts(
                entity.get("name"),
                entity.get("korean_name"),
                entity.get("physical_table"),
                entity.get("canonical_dimensions", []),
                entity.get("canonical_facts", []),
                entity.get("use_when", []),
            )
        )
    contract_texts = []
    for contract in schema.get("semantic_query_contracts", []):
        if str(contract.get("domain") or "") != str(domain_name):
            continue
        contract_texts.append(
            _join_texts(
                contract.get("name"),
                contract.get("description"),
                contract.get("match", {}),
                contract.get("metric_names", []),
                contract.get("source_tables", []),
                contract.get("entity_binding", {}),
                contract.get("time_policy", {}),
            )
        )
    attribute_texts = []
    for attribute in schema.get("semantic_attributes", []):
        domains = attribute.get("domains", attribute.get("domain", []))
        if isinstance(domains, str):
            domains = [domains]
        if domains and domain_name not in domains:
            continue
        attribute_texts.append(
            _join_texts(
                attribute.get("name"),
                attribute.get("korean_name"),
                attribute.get("aliases", []),
                attribute.get("business_definition"),
                attribute.get("source_mappings", []),
                attribute.get("value_semantics", {}),
            )
        )
    return _join_texts(
        domain.get("name"),
        domain.get("business_scope"),
        domain.get("keywords", []),
        domain.get("primary_entities", []),
        domain.get("preferred_metrics", []),
        metric_texts,
        entity_texts,
        contract_texts,
        attribute_texts,
    )


def _keyword_rule_domain_scores(schema: dict, question: str) -> dict[str, float]:
    q_lower = question.lower()
    q_tokens = _tokenize_text(question)
    scores: dict[str, float] = {}
    for domain in schema.get("canonical_domains", []):
        name = domain.get("name", "")
        if not name:
            continue
        score = 0.0
        for keyword in domain.get("keywords", []):
            keyword_lower = str(keyword).lower()
            if keyword_lower and keyword_lower in q_lower:
                score += 6.0
            keyword_tokens = _tokenize_text(str(keyword))
            if keyword_tokens and q_tokens.intersection(keyword_tokens):
                score += 2.0 * len(q_tokens.intersection(keyword_tokens))
        if name.lower() in q_lower:
            score += 8.0
        score += min(_score_by_question(question, domain.get("business_scope", "")), 8) * 0.7
        for metric_name in domain.get("preferred_metrics", []):
            if str(metric_name).lower() in q_lower:
                score += 5.0
        scores[name] = score
    return scores


def _metric_entity_domain_scores(schema: dict, question: str) -> dict[str, float]:
    q_lower = question.lower()
    q_tokens = _tokenize_text(question)
    scores: dict[str, float] = {}
    entity_index = _entity_by_name(schema)
    for metric in schema.get("canonical_metrics", []):
        domain = metric.get("domain", "")
        if not domain:
            continue
        score = scores.get(domain, 0.0)
        metric_name = str(metric.get("name", "")).lower()
        if metric_name and metric_name in q_lower:
            score += 10.0
        for synonym in metric.get("synonyms", []):
            synonym_lower = str(synonym).lower()
            if synonym_lower and synonym_lower in q_lower:
                score += 7.0
            synonym_tokens = _tokenize_text(str(synonym))
            if synonym_tokens and q_tokens.intersection(synonym_tokens):
                score += 2.0 * len(q_tokens.intersection(synonym_tokens))
        score += min(_score_by_question(question, metric.get("name", ""), metric.get("unit", "")), 5) * 0.8
        scores[domain] = score

    for domain in schema.get("canonical_domains", []):
        domain_name = domain.get("name", "")
        score = scores.get(domain_name, 0.0)
        for entity_name in domain.get("primary_entities", []):
            entity = entity_index.get(entity_name, {})
            entity_terms = [
                entity.get("name", ""),
                entity.get("korean_name", ""),
                entity.get("physical_table", ""),
                *entity.get("canonical_dimensions", []),
                *entity.get("canonical_facts", []),
                *_join_texts(entity.get("use_when", [])).split(),
            ]
            for term in entity_terms:
                term_lower = str(term).lower()
                if term_lower and term_lower in q_lower:
                    score += 3.0
            entity_token_overlap = q_tokens.intersection(_tokenize_text(_join_texts(entity_terms)))
            score += min(len(entity_token_overlap), 8) * 0.8
        scores[domain_name] = score
    return scores


def _weighted_domain_scores(
    keyword_scores: dict[str, float],
    metric_entity_scores: dict[str, float],
    embedding_scores: dict[str, float] | None = None,
) -> list[dict]:
    embedding_scores = embedding_scores or {}
    domain_names = set(keyword_scores) | set(metric_entity_scores) | set(embedding_scores)
    candidates = []
    for name in domain_names:
        keyword = keyword_scores.get(name, 0.0)
        metric_entity = metric_entity_scores.get(name, 0.0)
        embedding = embedding_scores.get(name, 0.0)
        total = keyword * 0.45 + metric_entity * 0.40 + max(embedding, 0.0) * 15.0
        candidates.append({
            "domain": name,
            "score": round(total, 4),
            "keyword_score": round(keyword, 4),
            "metric_entity_score": round(metric_entity, 4),
            "embedding_score": round(embedding, 4),
        })
    candidates.sort(key=lambda item: item["score"], reverse=True)
    return candidates


def _needs_domain_adjudication(candidates: list[dict]) -> bool:
    if not candidates:
        return False
    if len(candidates) == 1:
        return candidates[0]["score"] <= 0
    top = candidates[0]["score"]
    second = candidates[1]["score"]
    if top <= 0:
        return True
    if top < 5.0:
        return True
    return (top - second) <= max(2.0, top * 0.12)


def _adjudicate_domain_with_llm(question: str, candidates: list[dict], schema: dict) -> str:
    top_candidates = candidates[:3]
    if not top_candidates:
        return ""
    domain_lines = []
    domain_index = _domain_by_name(schema)
    for candidate in top_candidates:
        domain = domain_index.get(candidate["domain"], {})
        domain_lines.append(
            f"- {candidate['domain']}: score={candidate['score']}, "
            f"scope={domain.get('business_scope', '')}, "
            f"keywords={', '.join(domain.get('keywords', []))}, "
            f"metrics={', '.join(domain.get('preferred_metrics', []))}"
        )
    prompt = f"""사용자의 질문을 가장 적절한 데이터 도메인 하나로 분류하세요.
아래 후보 중 하나의 도메인명만 반환하세요. 후보 밖의 도메인은 반환하지 마세요.

## 후보 도메인
{chr(10).join(domain_lines)}

## 판단 규칙
1. 사용자가 묻는 공식 지표가 있는 도메인을 우선합니다.
2. 지표가 없으면 주 분석 대상 엔티티가 있는 도메인을 우선합니다.
3. 두 도메인이 모두 필요해도 SQL의 시작점이 되는 주 도메인 하나를 고릅니다.

## 사용자 질문
{question}

도메인명:"""
    try:
        raw = _normalize_llm_text(_call_llm(prompt, max_tokens=128))
    except Exception:
        return top_candidates[0]["domain"]
    for candidate in top_candidates:
        if candidate["domain"] in raw:
            return candidate["domain"]
    return top_candidates[0]["domain"]


def _schema_table_index(schema: dict) -> tuple[dict[str, dict], dict[str, set[str]]]:
    tables = {}
    columns = {}
    for table in schema.get("tables", []):
        physical = table.get("physical_table", table.get("name", ""))
        logical = table.get("name", physical)
        if not physical:
            continue
        tables[physical.lower()] = table
        tables[logical.lower()] = table
        col_set = set()
        for section in ("dimensions", "measures", "time_dimensions"):
            for col in table.get(section, []):
                if col.get("name"):
                    col_set.add(col["name"].lower())
                expr = col.get("expr", "")
                for token in re.findall(r"\b[a-zA-Z가-힣_][a-zA-Z0-9가-힣_]*\b", expr):
                    col_set.add(token.lower())
        columns[physical.lower()] = col_set
        columns[logical.lower()] = col_set
    return tables, columns


_SQL_ALIAS_STOP_WORDS = {
    "where", "join", "left", "right", "full", "inner", "outer", "cross",
    "on", "group", "order", "having", "limit", "union", "except", "intersect",
}


def _validate_qualified_columns(sql: str) -> list[str]:
    """Validate quoted ``alias.\"column\"`` references for physical tables.

    Full SQL parsing is intentionally left to Athena, but verified/generated SQL
    overwhelmingly qualifies source columns.  Tracking every physical table that
    an alias can denote catches document/schema drift without mistaking CTE output
    columns for source columns.  Multiple scopes may reuse the same alias, so a
    column is accepted when it exists on at least one physical table bound to it.
    """
    known_tables, known_columns = _schema_table_index(SCHEMA)
    cte_names = _extract_cte_names(sql)
    alias_tables: dict[str, set[str]] = {}
    cte_aliases: set[str] = set()
    table_pattern = re.compile(
        r'\b(?:FROM|JOIN)\s+'
        r'(?:(?:"?[A-Za-z_][A-Za-z0-9_]*"?)\.)?'
        r'"?([A-Za-z_][A-Za-z0-9_]*)"?'
        r'(?:\s+(?:AS\s+)?"?([A-Za-z_][A-Za-z0-9_]*)"?)?',
        re.IGNORECASE,
    )
    for match in table_pattern.finditer(sql):
        table_name = match.group(1).lower()
        alias = (match.group(2) or table_name).lower()
        if alias in _SQL_ALIAS_STOP_WORDS:
            alias = table_name
        if table_name in cte_names:
            # A CTE output may intentionally reuse an alias that a physical
            # table used in another SQL scope. Its derived columns cannot be
            # checked against the physical schema without a full scope parser.
            cte_aliases.add(alias)
            continue
        if table_name not in known_tables:
            continue
        alias_tables.setdefault(alias, set()).add(table_name)
        alias_tables.setdefault(table_name, set()).add(table_name)

    issues = []
    seen: set[tuple[str, str]] = set()
    for alias, column_name in re.findall(
        r'\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*"([^"]+)"',
        sql,
    ):
        if alias.lower() in cte_aliases:
            continue
        candidate_tables = alias_tables.get(alias.lower(), set())
        if not candidate_tables:
            continue
        column_lower = column_name.lower()
        key = (alias.lower(), column_name)
        if key in seen:
            continue
        seen.add(key)
        table_labels = ", ".join(sorted(candidate_tables))
        if _SENSITIVE_COLUMN_RE.search(column_name) or any(
            _is_restricted_column(known_tables.get(table_name, {}), column_name)
            for table_name in candidate_tables
        ):
            issues.append(
                f"제한된 민감 컬럼 {alias}.\"{column_name}\" 은 조회할 수 없습니다. "
                f"(테이블: {table_labels})"
            )
            continue
        if any(column_lower in known_columns.get(table_name, set()) for table_name in candidate_tables):
            continue
        issues.append(f"스키마에 없는 컬럼 {alias}.\"{column_name}\" 이 사용되었습니다. (테이블: {table_labels})")
    return issues


# 설정된 스키마 prefix 기준으로 테이블을 추출/검증한다 (예: "card_system." 또는 "").
# prefix가 비어 있으면(스키마 미사용) FROM/JOIN의 테이블명을 직접 본다.
_SCHEMA_TABLE_RE = (
    re.compile(rf"\b{re.escape(DB_SCHEMA)}\.([a-zA-Z0-9_]+)\b") if DB_SCHEMA else None
)


def _extract_cte_names(sql: str) -> set[str]:
    """Return names introduced by a WITH clause so they are not treated as tables."""
    cte_names = {
        (match.group(1) or match.group(2)).lower()
        for match in re.finditer(
            r'(?:\bWITH|,)\s+(?:"([^"]+)"|([a-zA-Z_][a-zA-Z0-9_]*))\s*(?:\([^)]*\))?\s+AS\s*\(',
            sql,
            re.IGNORECASE,
        )
        if match.group(1) or match.group(2)
    }
    # Some SQL formatters/model outputs can insert unusual spacing around WITH
    # items. Keep a conservative fallback for any identifier directly followed
    # by AS ( ... ), which is the CTE shape that can later appear in JOIN.
    cte_names.update(
        (match.group(1) or match.group(2)).lower()
        for match in re.finditer(
            r'(?:"([^"]+)"|([a-zA-Z_][a-zA-Z0-9_]*))\s*(?:\([^)]*\))?\s+AS\s*\(',
            sql,
            re.IGNORECASE,
        )
        if match.group(1) or match.group(2)
    )
    return cte_names


def _extract_schema_tables(sql: str) -> set[str]:
    cte_names = _extract_cte_names(sql)
    # FROM/JOIN 실물 테이블을 하나의 패턴으로 추출한다. 예전
    # DB_SCHEMA 전용 regex는 card_system."tbdaaus01" 처럼 테이블명을
    # 인용한 SQL을 놓쳐 적재 주기·민감 컬럼 검증을 통째로 우회했다.
    table_pattern = re.compile(
        r'\b(?:FROM|JOIN)\s+'
        r'(?:(?:"(?P<quoted_schema>[A-Za-z_][A-Za-z0-9_]*)"|'
        r'(?P<plain_schema>[A-Za-z_][A-Za-z0-9_]*))\s*\.\s*)?'
        r'(?:"(?P<quoted_table>[A-Za-z_][A-Za-z0-9_]*)"|'
        r'(?P<plain_table>[A-Za-z_][A-Za-z0-9_]*))',
        re.IGNORECASE,
    )
    tables: set[str] = set()
    for match in table_pattern.finditer(sql or ""):
        table_name = (match.group("quoted_table") or match.group("plain_table")).lower()
        schema_name = (match.group("quoted_schema") or match.group("plain_schema") or "").lower()
        if table_name in {"select", *cte_names} and not schema_name:
            continue
        if _SCHEMA_TABLE_RE is not None and schema_name and schema_name != DB_SCHEMA.lower():
            # 다른 스키마의 동명 테이블을 현재 semantic schema로
            # 잘못 검증하지 않는다.
            continue
        tables.add(table_name)
    return tables


_SELECT_STAR_SOURCE_RE = re.compile(
    r'\bSELECT\s+\*\s+FROM\s+'
    r'(?:(?P<schema>"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*)?'
    r'(?:"(?P<quoted>[A-Za-z_][A-Za-z0-9_]*)"|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))',
    re.IGNORECASE,
)


def _has_unsafe_select_star(sql: str, known_tables: dict[str, dict]) -> bool:
    """Reject result wildcards and wildcards that read a physical table."""
    statements = sqlparse.parse(_sql_identifier_view(sql))
    if statements:
        in_select = False
        for token in statements[0].tokens:
            if not in_select:
                if token.ttype is sqlparse.tokens.DML and token.normalized == "SELECT":
                    in_select = True
                continue
            if token.ttype in sqlparse.tokens.Keyword and token.normalized == "FROM":
                break
            if token.ttype is sqlparse.tokens.Wildcard:
                return True

    cte_names = _extract_cte_names(sql)
    for match in _SELECT_STAR_SOURCE_RE.finditer(sql):
        table = (match.group("quoted") or match.group("plain")).lower()
        if table in known_tables and (match.group("schema") or table not in cte_names):
            return True
    return False


def _validate_sql_against_schema(sql: str, selected_tables: list[str]) -> list[str]:
    issues = []
    stripped = sqlparse.format(sql or "", strip_comments=True).strip()
    if not stripped:
        return ["SQL 파싱 실패: 빈 쿼리입니다."]
    safety_error = _validate_read_only_sql(stripped)
    if safety_error:
        issues.append(safety_error)

    known_tables, _ = _schema_table_index(SCHEMA)
    used_tables = _extract_schema_tables(stripped)
    unknown_tables = sorted(table for table in used_tables if table not in known_tables)
    for table in unknown_tables:
        issues.append(f"스키마에 없는 테이블 {DB_SCHEMA_PREFIX}{table} 이 사용되었습니다.")
    restricted_tables = sorted(
        table
        for table in used_tables
        if table in known_tables and not _is_semantic_table_visible(known_tables[table])
    )
    for table in restricted_tables:
        issues.append(f"접근이 제한된 테이블 {DB_SCHEMA_PREFIX}{table} 은 조회할 수 없습니다.")
    issues.extend(_validate_qualified_columns(stripped))

    # 스키마 prefix가 설정된 경우에만 prefix 누락을 검증한다.
    if DB_SCHEMA_PREFIX:
        prefix_lower = DB_SCHEMA_PREFIX.lower()
        for table in selected_tables:
            table_lower = str(table).lower()
            if table_lower.startswith(prefix_lower):
                table_lower = table_lower[len(prefix_lower):]
            if len(table_lower) < 4:
                continue
            if table_lower in stripped.lower() and f"{prefix_lower}{table_lower}" not in stripped.lower():
                issues.append(f"테이블 '{table}'에 {DB_SCHEMA_PREFIX} prefix가 없습니다.")

        for match in re.finditer(r"\b(FROM|JOIN)\s+([a-zA-Z0-9_]+)\b", stripped, re.IGNORECASE):
            table = match.group(2)
            if table.lower() != "select" and f"{prefix_lower}{table.lower()}" not in stripped.lower():
                if table.lower() in known_tables:
                    issues.append(f"테이블 '{table}'은 {DB_SCHEMA_PREFIX}{table} 형태로 사용해야 합니다.")

    if _has_unsafe_select_star(stripped, known_tables):
        issues.append("SELECT * 대신 필요한 컬럼만 명시하세요.")
    return issues


def _result_summary(
    columns: list,
    rows: list[tuple],
    max_top_values: int = 5,
    total_row_count: int | None = None,
) -> str:
    if not columns or not rows:
        return "(조회 데이터 없음)"
    # 합계·평균은 넘겨받은 행만 집계한 값이다. 원본이 더 크면 그 사실을 같이
    # 적어야 부분 합계가 전체 합계로 답변에 실리지 않는다.
    # total_row_count=None 은 조회 한도에 걸려 전체 건수를 세지 못한 경우다.
    if total_row_count == len(rows):
        lines = [f"- row_count: {len(rows)} (전체 건수)"]
    else:
        lines = [
            f"- row_count: {total_row_count} (전체 건수)"
            if total_row_count is not None
            else "- row_count: 조회 한도에 걸려 미확인 (건수는 결과 범위 안내를 따를 것)",
            f"- 아래 합계·평균·최소·최대는 상위 {len(rows)}행만 집계한 값입니다.",
        ]
    for idx, column in enumerate(columns):
        values = [row[idx] for row in rows if idx < len(row) and row[idx] is not None]
        numeric_values = [float(v) for v in values if isinstance(v, (int, float, Decimal))]
        if numeric_values:
            lines.append(
                f"- {column}: 합계={sum(numeric_values):,.4f}, 평균={sum(numeric_values) / len(numeric_values):,.4f}, "
                f"최소={min(numeric_values):,.4f}, 최대={max(numeric_values):,.4f}"
            )
        elif values:
            counts: dict[str, int] = {}
            for value in values:
                key = str(value)
                counts[key] = counts.get(key, 0) + 1
            top = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:max_top_values]
            lines.append(f"- {column}: 주요값 " + ", ".join(f"{k}({v})" for k, v in top))
    return "\n".join(lines)



def _merge_external_verified_queries(schema: dict) -> dict:
    extra_queries = load_external_verified_queries()
    if not extra_queries:
        return schema
    known_tables = {
        str(name).lower()
        for table in schema.get("tables", [])
        for name in (
            table.get("name", ""),
            str(table.get("physical_table", "")).rsplit(".", 1)[-1],
        )
        if name
    }
    enabled_queries = []
    disabled_queries = []
    table_pattern = re.compile(
        r'\b(?:FROM|JOIN)\s+(?:(?:[A-Za-z_][A-Za-z0-9_]*|"[^"]+")\.)?"?([A-Za-z_][A-Za-z0-9_]*)"?',
        re.IGNORECASE,
    )
    for query in extra_queries:
        sql = str(query.get("sql") or "")
        cte_names = _extract_cte_names(sql)
        used_tables = {
            match.group(1).lower()
            for match in table_pattern.finditer(sql)
            if match.group(1).lower() not in cte_names
            and match.group(1).lower() not in {"select", "unnest", "values"}
        }
        unknown_tables = sorted(used_tables - known_tables)
        if unknown_tables:
            disabled_queries.append(
                {
                    "name": query.get("name", ""),
                    "reason": "semantic schema에 없는 테이블 참조",
                    "unknown_tables": unknown_tables,
                }
            )
            continue
        enabled_queries.append(query)
    merged = dict(schema)
    # Keep verified query definitions in one authoritative file. The semantic
    # schema may still contain legacy copies, but runtime matching uses the
    # external file whenever it is present.
    merged["verified_queries"] = enabled_queries
    merged["disabled_verified_queries"] = disabled_queries
    return merged


SCHEMA = _merge_external_verified_queries(load_semantic_layer())
VERIFIED_QUERIES = SCHEMA.get("verified_queries", [])

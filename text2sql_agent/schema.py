"""Semantic schema loading, prompt context builders, and schema validation."""

import json
import os
import re
from decimal import Decimal

import sqlparse
import yaml

from .config import SCHEMA_PATH
from .db import _DANGEROUS_SQL_RE
from .llm import _call_llm

# ---------------------------------------------------------------------------
# 3. Semantic Layer 로더
# ---------------------------------------------------------------------------

def load_semantic_layer(path: str | None = None) -> dict:
    if path is None:
        path = os.getenv("SEMANTIC_SCHEMA_PATH", str(SCHEMA_PATH))
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_table_summary(schema: dict) -> str:
    lines = []
    for t in schema.get("tables", []):
        name = t["name"]
        desc = t.get("description", "").strip().replace("\n", " ")
        grain = t.get("grain", "")
        lines.append(f"- **{name}** ({t['physical_table']}): {desc} [grain: {grain}]")
        dims = t.get("dimensions", [])
        if dims:
            dim_names = [d["name"] for d in dims[:8]]
            lines.append(f"  dimensions: {', '.join(dim_names)}" + (" ..." if len(dims) > 8 else ""))
        measures = t.get("measures", [])
        if measures:
            m_names = [m["name"] for m in measures[:6]]
            lines.append(f"  measures: {', '.join(m_names)}" + (" ..." if len(measures) > 6 else ""))
        tds = t.get("time_dimensions", [])
        if tds:
            lines.append(f"  time_dimensions: {', '.join(td['name'] for td in tds)}")
        filters = t.get("filters", [])
        if filters:
            f_strs = [fl["name"] + ": " + fl["expr"] for fl in filters]
            lines.append(f"  filters: {'; '.join(f_strs)}")
        lines.append("")
    return "\n".join(lines)


def build_metrics_summary(schema: dict) -> str:
    lines = []
    for m in schema.get("metrics", []):
        synonyms = ", ".join(m.get("synonyms", []))
        lines.append(f"- **{m['name']}**: {m['description'].strip()}")
        lines.append(f"  SQL: {m['sql'].strip()}")
        lines.append(f"  source: {m['source_table']}, synonyms: [{synonyms}]")
        lines.append("")
    return "\n".join(lines)


def build_glossary_summary(schema: dict) -> str:
    lines = []
    for g in schema.get("glossary", []):
        aliases = ", ".join(g.get("aliases", []))
        lines.append(f"- **{g['term']}** ({g['canonical']}): {g['description']}")
        if aliases:
            lines.append(f"  aliases: [{aliases}]")
        if g.get("sql_hint"):
            lines.append(f"  sql_hint: {g['sql_hint']}")
    return "\n".join(lines)


_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣_]+")


def _tokenize_text(text: str) -> set[str]:
    tokens = set()
    for token in _TOKEN_RE.findall((text or "").lower()):
        if len(token) >= 2:
            tokens.add(token)
    return tokens


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


def _format_query_reference(ref: dict) -> str:
    lines = [f"- **{ref.get('intent', '')}**"]
    says = ", ".join(ref.get("when_user_says", [])[:4])
    if says:
        lines.append(f"  user_says: [{says}]")
    if ref.get("primary_table"):
        lines.append(f"  primary_table: {ref['primary_table']}")
    if ref.get("join_tables"):
        lines.append(f"  join_tables: {', '.join(ref.get('join_tables', []))}")
    if ref.get("join_rule"):
        lines.append(f"  join_rule: {ref['join_rule']}")
    cols = ref.get("recommended_columns", {})
    if cols:
        lines.append("  column_hints:")
        for key, value in cols.items():
            lines.append(f"    - {key}: {value}")
    rules = ref.get("rules", [])
    if rules:
        lines.append("  rules:")
        for rule in rules[:5]:
            lines.append(f"    - {rule}")
    if ref.get("sql_pattern"):
        lines.append(f"  sql_pattern:\n{ref['sql_pattern'].strip()}")
    return "\n".join(lines)


def find_relevant_references(schema: dict, question: str, max_count: int = 4) -> str:
    refs = schema.get("query_references", [])
    if not refs:
        return "(관련 reference 없음)"
    scored = []
    q_lower = question.lower()
    for ref in refs:
        score = _score_by_question(
            question,
            ref.get("intent", ""),
            " ".join(ref.get("when_user_says", [])),
            ref.get("primary_table", ""),
            " ".join(ref.get("join_tables", [])),
            json.dumps(ref.get("recommended_columns", {}), ensure_ascii=False),
            " ".join(ref.get("rules", [])),
        )
        for phrase in ref.get("when_user_says", []):
            phrase_lower = str(phrase).lower().strip()
            if phrase_lower and phrase_lower in q_lower:
                score += 4
        if ref.get("primary_table") and ref["primary_table"].lower() in q_lower:
            score += 3
        scored.append((score, ref))
    scored.sort(key=lambda item: item[0], reverse=True)
    top = [ref for score, ref in scored[:max_count] if score > 0]
    if not top:
        top = [ref for _, ref in scored[: min(2, len(scored))]]
    return "\n\n".join(_format_query_reference(ref) for ref in top)


def find_relevant_queries(schema: dict, question: str, max_count: int = 3) -> str:
    vqs = schema.get("verified_queries", [])
    scored = []
    q_lower = question.lower()
    for vq in vqs:
        score = _score_by_question(
            question,
            vq.get("question", ""),
            vq.get("description", ""),
            " ".join(vq.get("tags", [])),
            json.dumps(vq.get("parameters", {}), ensure_ascii=False),
        )
        for tag in (t.lower() for t in vq.get("tags", [])):
            if tag in q_lower:
                score += 3
        scored.append((score, vq))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [vq for s, vq in scored[:max_count] if s > 0]
    if not top:
        top = [vq for _, vq in scored[:2]]
    lines = []
    for vq in top:
        lines.append(f"Q: {vq['question']}")
        lines.append(f"SQL:\n{vq['sql'].strip()}\n")
    return "\n".join(lines)


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


def build_semantic_contract_summary(schema: dict) -> str:
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


def _semantic_join_paths_for_domain(schema: dict, domain_name: str, question: str = "", max_paths: int = 8) -> list[dict]:
    domain = _domain_by_name(schema).get(domain_name, {})
    primary_entities = set(domain.get("primary_entities", []))
    if not primary_entities:
        return []
    q_tokens = _tokenize_text(question)
    scored = []
    for path in schema.get("semantic_join_graph", {}).get("safe_paths", []):
        from_entity = path.get("from_entity")
        to_entity = path.get("to_entity")
        touches_domain = from_entity in primary_entities or to_entity in primary_entities
        if not touches_domain:
            continue
        score = 2.0
        if from_entity in primary_entities and to_entity in primary_entities:
            score += 3.0
        use_when_text = _join_texts(path.get("use_when", []), path.get("name", ""), path.get("caution", ""))
        overlap = q_tokens.intersection(_tokenize_text(use_when_text))
        score += len(overlap)
        scored.append((score, path))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in scored[:max_paths]]


def build_semantic_join_context(schema: dict, domain_name: str, question: str = "", max_paths: int = 8) -> str:
    paths = _semantic_join_paths_for_domain(schema, domain_name, question, max_paths=max_paths)
    if not paths:
        return "(선택 도메인에 연결된 semantic_join_graph.safe_paths 없음)"
    entity_index = _entity_by_name(schema)
    lines = ["Use only these safe semantic join paths when they fit the question:"]
    for path in paths:
        from_entity = path.get("from_entity", "")
        to_entity = path.get("to_entity", "")
        from_table = entity_index.get(from_entity, {}).get("physical_table", "")
        to_table = entity_index.get(to_entity, {}).get("physical_table", "")
        lines.append(
            f"- {path.get('name')}: {from_entity}({from_table}) -> {to_entity}({to_table}), "
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
    metrics = _canonical_metrics_by_domain(schema).get(domain_name, [])[:max_metrics]
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
    join_context = build_semantic_join_context(schema, domain_name)
    if join_context:
        lines.append("- semantic_join_graph:")
        lines.append(join_context)
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
    return _join_texts(
        domain.get("name"),
        domain.get("business_scope"),
        domain.get("keywords", []),
        domain.get("primary_entities", []),
        domain.get("preferred_metrics", []),
        metric_texts,
        entity_texts,
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
        raw = _call_llm(prompt).strip()
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


def _extract_schema_tables(sql: str) -> set[str]:
    return {match.group(1).lower() for match in re.finditer(r"\bcard_system\.([a-zA-Z0-9_]+)\b", sql)}


def _validate_sql_against_schema(sql: str, selected_tables: list[str]) -> list[str]:
    issues = []
    stripped = sqlparse.format(sql or "", strip_comments=True).strip()
    if not stripped:
        return ["SQL 파싱 실패: 빈 쿼리입니다."]
    first_token = stripped.split(None, 1)[0].upper() if stripped.split() else ""
    if first_token not in {"SELECT", "WITH"}:
        issues.append("읽기 전용 SELECT/WITH 쿼리만 실행할 수 있습니다.")
    if _DANGEROUS_SQL_RE.search(stripped):
        issues.append("INSERT/UPDATE/DELETE/DDL 계열 명령은 사용할 수 없습니다.")

    known_tables, _ = _schema_table_index(SCHEMA)
    used_tables = _extract_schema_tables(stripped)
    unknown_tables = sorted(table for table in used_tables if table not in known_tables)
    for table in unknown_tables:
        issues.append(f"스키마에 없는 테이블 card_system.{table} 이 사용되었습니다.")

    for table in selected_tables:
        table_lower = str(table).lower()
        if len(table_lower) < 4:
            continue
        if table_lower in stripped.lower() and f"card_system.{table_lower}" not in stripped.lower():
            issues.append(f"테이블 '{table}'에 card_system. prefix가 없습니다.")

    for match in re.finditer(r"\b(FROM|JOIN)\s+([a-zA-Z0-9_]+)\b", stripped, re.IGNORECASE):
        table = match.group(2)
        if table.lower() != "select" and f"card_system.{table.lower()}" not in stripped.lower():
            if table.lower() in known_tables:
                issues.append(f"테이블 '{table}'은 card_system.{table} 형태로 사용해야 합니다.")

    if re.search(r"\bSELECT\s+\*", stripped, re.IGNORECASE):
        issues.append("SELECT * 대신 필요한 컬럼만 명시하세요.")
    return issues


def _result_summary(columns: list, rows: list[tuple], max_top_values: int = 5) -> str:
    if not columns or not rows:
        return "(조회 데이터 없음)"
    lines = [f"- row_count: {len(rows)}"]
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



SCHEMA = load_semantic_layer()
VERIFIED_QUERIES = SCHEMA.get("verified_queries", [])

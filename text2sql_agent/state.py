"""LangGraph state contract for the Text2SQL workflow."""

from typing import TypedDict

# ---------------------------------------------------------------------------
# 8. State 정의
# ---------------------------------------------------------------------------

class Text2SQLState(TypedDict):
    question: str
    retrieval_query: str
    question_type: str
    # --- Prompt-based safety guardrail ---
    safety_action: str
    safety_category: str
    safety_reason_code: str
    safety_direction: str
    # --- Multi-turn context (optional values, initialized as empty strings) ---
    previous_question: str
    previous_sql: str
    previous_answer: str
    followup_question: str
    query_frame: dict
    # --- Domain Routing ---
    selected_domain: str
    domain_candidates: list[dict]
    domain_routing_trace: str
    domain_context: str
    # --- Tool ---
    selected_tool: str
    tool_params: dict
    tool_completed: bool
    skip_tool_selection: bool
    selected_capability_type: str
    selected_capability_name: str
    # --- 범용 파라미터 프롬프트 ---
    missing_params: list
    param_stage: str
    user_provided_params: dict
    # --- 되묻기(clarification) ---
    # 사용자가 선택지 중 하나를 고른 결과를 프롬프트에 그대로 전달하기 위한
    # 확정 해석 문장들. 값 자체는 user_provided_params/selected_domain에 반영된다.
    clarification_directives: list
    # --- Verified Query ---
    matched_query_name: str
    matched_query_sql: str
    matched_query_params: dict
    extracted_params: dict
    skip_verified_query_matching: bool
    # --- SQL 생성 ---
    selected_tables: list[str]
    table_details: str
    generated_sql: str
    validation_result: str
    is_valid: bool
    retry_count: int
    final_sql: str
    implicit_time_basis: str
    # --- 공통 결과 ---
    query_columns: list[str]
    query_rows: list[tuple]
    query_error: str
    result_scope: dict
    answer: str
    error_message: str
    # --- 대손비용률 결과 ---
    bad_debt_excel_path: str

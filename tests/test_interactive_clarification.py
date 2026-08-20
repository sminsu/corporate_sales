"""The agent asks instead of guessing, and a picked option resumes the run."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import web_service
from text2sql_agent import clarify, workflow


def _param(name: str, **extra) -> dict:
    return clarify.upgrade_param({"name": name, "label": name, "type": "string", **extra}, today=date(2026, 8, 20))


# ---------------------------------------------------------------------------
# Questions carry their own options
# ---------------------------------------------------------------------------

def test_required_period_param_offers_calendar_options_instead_of_a_blank_field() -> None:
    param = _param("기준년월")

    assert param["kind"] == "choice"
    assert [option["label"] for option in param["options"]] == ["전월", "이번 달", "2개월 전", "작년 동월"]
    assert [option["value"] for option in param["options"]] == ["202607", "202608", "202606", "202508"]
    assert param["allow_free_text"] is True


def test_basis_date_param_offers_day_level_options() -> None:
    param = _param("기준년월일")

    assert [option["value"] for option in param["options"]] == ["20260731", "20260820", "20260831"]


def test_non_period_param_stays_a_free_text_question() -> None:
    param = _param("월매출금액")

    assert param["kind"] == "text"
    assert param["options"] == []
    assert param["allow_free_text"] is True


def test_verified_query_missing_period_reaches_the_user_as_a_question() -> None:
    question = "KB카드 기업카드 보유회원 중에 6개월 무실적인 기업 회원 명단을 알려줘"
    state = {"question": question, "domain_context": "", "user_provided_params": {}}
    state.update(workflow.select_tool(state))

    with patch.object(workflow, "_call_llm", return_value="{}"):
        result = workflow.extract_and_apply_params(state)

    assert result["param_stage"] == "need_params"
    [param] = [item for item in result["missing_params"] if item["name"] == "기준년월"]
    assert param["kind"] == "choice"
    assert param["options"]
    assert param["reason"]


def test_duplicate_code_label_asks_which_axis_with_selectable_axes() -> None:
    result = workflow.check_sql_gen_params({"question": "기타 고객 목록", "query_frame": {}})

    [param] = result["missing_params"]
    assert param["name"] == "기타분류축"
    assert param["kind"] == "choice"
    assert param["allow_free_text"] is False
    assert len(param["options"]) >= 2
    for option in param["options"]:
        assert param["directives"][option["value"]]


def test_missing_target_name_explains_why_it_stopped() -> None:
    param = clarify.target_name_clarification().to_param()

    assert param["name"] == "대상명"
    assert param["kind"] == "text"
    assert "전체 조회로 넓히지 않았습니다" in param["reason"]
    assert param["free_text_hint"]


def test_clarification_can_be_turned_off_entirely() -> None:
    with patch.object(workflow, "ENABLE_INTERACTIVE_CLARIFICATION", False):
        params = workflow._as_clarifications([{"name": "기준년월", "label": "기준년월"}])

    assert params == [{"name": "기준년월", "label": "기준년월"}]


# ---------------------------------------------------------------------------
# Answers bind without a model call
# ---------------------------------------------------------------------------

def test_option_label_number_and_value_all_bind_to_the_same_answer() -> None:
    param = _param("기준년월")

    assert clarify.resolve_answer(param, "전월") == "202607"
    assert clarify.resolve_answer(param, "전 월") == "202607"
    assert clarify.resolve_answer(param, "1") == "202607"
    assert clarify.resolve_answer(param, "2번") == "202608"
    assert clarify.resolve_answer(param, "202607") == "202607"


def test_a_period_the_user_typed_out_is_left_to_the_existing_parsers() -> None:
    param = _param("기준년월")

    # 202601 is not on the option list, and must not be read as an index.
    assert clarify.resolve_answer(param, "202601") == ""
    assert clarify.resolve_answer(param, "2026년 1월") == ""


def test_picking_an_option_skips_the_model_and_the_rule_parser() -> None:
    continuation = {"question": "기업카드 이용금액 알려줘", "missing_params": [_param("기준년월")]}

    with patch.object(web_service, "_natural_params_by_llm") as llm:
        resolved = web_service._coerce_natural_params({"_natural_input": "전월"}, continuation)

    llm.assert_not_called()
    assert resolved["기준년월"] == "202607"


def test_a_free_form_reply_still_reaches_the_existing_parsers() -> None:
    continuation = {"question": "기업카드 이용금액 알려줘", "missing_params": [_param("기준년월")]}

    with patch.object(web_service, "_natural_params_by_llm", return_value={}) as llm:
        resolved = web_service._coerce_natural_params({"_natural_input": "2026년 1월로 해줘"}, continuation)

    assert resolved["기준년월"] == "202601"
    llm.assert_not_called()


def test_a_chosen_axis_becomes_an_explicit_instruction_for_sql_generation() -> None:
    result = workflow.check_sql_gen_params({"question": "기타 고객 목록", "query_frame": {}})
    [param] = result["missing_params"]
    chosen = param["options"][0]["value"]
    state: dict = {"clarification_directives": []}

    remaining = clarify.apply_answers(state, result["missing_params"], {param["name"]: chosen})

    assert remaining == {param["name"]: chosen}
    assert state["clarification_directives"] == [param["directives"][chosen]]
    assert "사용자가 확정한 해석" in clarify.directives_prompt(state["clarification_directives"])


# ---------------------------------------------------------------------------
# Routing questions
# ---------------------------------------------------------------------------

def test_tied_weak_routing_scores_ask_which_domain_to_use() -> None:
    candidates = [{"domain": "corporate_sales", "score": 3.0}, {"domain": "merchant", "score": 2.9}]

    state = workflow.check_domain_choice(
        {"selected_domain": "corporate_sales", "domain_candidates": candidates, "domain_routing_trace": ""}
    )

    assert state["param_stage"] == "need_params"
    [param] = state["missing_params"]
    assert param["name"] == "조회도메인"
    assert param["applies_to"] == "domain"
    # The agent's own pick leads the list so the question reads as a check.
    assert [option["value"] for option in param["options"]] == ["corporate_sales", "merchant"]


def test_a_confident_route_is_not_second_guessed() -> None:
    candidates = [{"domain": "corporate_sales", "score": 9.0}, {"domain": "merchant", "score": 8.9}]

    state = workflow.check_domain_choice(
        {"selected_domain": "corporate_sales", "domain_candidates": candidates, "domain_routing_trace": ""}
    )

    assert state == {"missing_params": [], "param_stage": "done"}


def test_a_clear_winner_among_weak_scores_is_not_second_guessed() -> None:
    candidates = [{"domain": "corporate_sales", "score": 3.0}, {"domain": "merchant", "score": 0.4}]

    state = workflow.check_domain_choice(
        {"selected_domain": "corporate_sales", "domain_candidates": candidates, "domain_routing_trace": ""}
    )

    assert state == {"missing_params": [], "param_stage": "done"}


def test_a_domain_the_user_already_chose_is_never_re_asked() -> None:
    candidates = [{"domain": "corporate_sales", "score": 3.0}, {"domain": "merchant", "score": 2.9}]

    state = workflow.check_domain_choice(
        {
            "selected_domain": "merchant",
            "domain_candidates": candidates,
            "domain_routing_trace": "top_candidates:\nuser_selected_domain=merchant",
        }
    )

    assert state == {"missing_params": [], "param_stage": "done"}


def test_a_chosen_domain_reroutes_the_run_instead_of_becoming_a_sql_parameter() -> None:
    candidates = [{"domain": "corporate_sales", "score": 3.0}, {"domain": "merchant", "score": 2.9}]
    asked = workflow.check_domain_choice(
        {"selected_domain": "corporate_sales", "domain_candidates": candidates, "domain_routing_trace": "top"}
    )
    state: dict = {"selected_domain": "corporate_sales", "domain_context": "예전 도메인 설명", "domain_routing_trace": "top"}

    remaining = clarify.apply_answers(state, asked["missing_params"], {"조회도메인": "merchant"})

    assert remaining == {}
    assert state["selected_domain"] == "merchant"
    assert state["domain_context"] == ""
    assert "user_selected_domain=merchant" in state["domain_routing_trace"]


def test_domain_clarification_respects_its_kill_switch() -> None:
    candidates = [{"domain": "corporate_sales", "score": 3.0}, {"domain": "merchant", "score": 2.9}]

    with patch.object(workflow, "ENABLE_DOMAIN_CLARIFICATION", False):
        state = workflow.check_domain_choice(
            {"selected_domain": "corporate_sales", "domain_candidates": candidates, "domain_routing_trace": ""}
        )

    assert state == {"missing_params": [], "param_stage": "done"}


# ---------------------------------------------------------------------------
# Resume plumbing
# ---------------------------------------------------------------------------

def test_resuming_with_a_domain_answer_re_enters_on_the_chosen_domain() -> None:
    candidates = [{"domain": "corporate_sales", "score": 3.0}, {"domain": "merchant", "score": 2.9}]
    asked = workflow.check_domain_choice(
        {"selected_domain": "corporate_sales", "domain_candidates": candidates, "domain_routing_trace": "top"}
    )
    continuation = {
        "question": "이용금액 알려줘",
        "selected_domain": "corporate_sales",
        "domain_candidates": candidates,
        "domain_routing_trace": "top",
        "domain_context": "corporate_sales 설명",
        "missing_params": asked["missing_params"],
    }
    request = web_service.CompatibleQueryRequest(
        query="이용금액 알려줘", params={"조회도메인": "merchant"}, continuation=continuation
    )

    state = web_service._state_from_request(request, {"id": "s1"})

    assert state["selected_domain"] == "merchant"
    assert state["user_provided_params"] == {}
    assert state["clarification_directives"] == ["조회 도메인은 merchant으로 확정한다."]


def test_the_assistant_message_lists_the_options_it_is_offering() -> None:
    answer = web_service._requires_params_answer({"missing_params": [_param("기준년월")]})

    assert "진행 방향을 확인하고 싶어요." in answer
    assert "1) 전월 = 202607" in answer
    assert "보기 번호나 값을 그대로 답해" in answer


def test_a_plain_required_value_keeps_the_original_instruction_wording() -> None:
    answer = web_service._requires_params_answer({"missing_params": [_param("월매출금액")]})

    assert "추가 입력이 필요해요." in answer
    assert "필요한 값:" in answer


def test_a_chat_reply_of_an_option_label_resumes_the_verified_query_end_to_end() -> None:
    question = "KB카드 기업카드 보유회원 중에 6개월 무실적인 기업 회원 명단을 알려줘"
    state = {"question": question, "domain_context": "", "user_provided_params": {}}
    state.update(workflow.select_tool(state))
    with patch.object(workflow, "_call_llm", return_value="{}"):
        paused = workflow.extract_and_apply_params(state)
    paused["question"] = question
    assert paused["param_stage"] == "need_params"

    continuation = web_service._build_continuation({**state, **paused})
    [asked] = [item for item in continuation["missing_params"] if item["name"] == "기준년월"]
    picked = asked["options"][0]

    session = {"id": "s1", "pending_continuation": continuation}
    request = web_service.CompatibleQueryRequest(query=picked["label"])
    with patch.object(web_service, "_natural_params_by_llm") as llm:
        resumed = web_service._state_from_request(request, session)
    llm.assert_not_called()

    assert resumed["user_provided_params"]["기준년월"] == picked["value"]

    with patch.object(workflow, "_call_llm", return_value="{}"):
        finished = workflow.extract_and_apply_params({**resumed, "question": question})

    assert finished["param_stage"] == "done"
    assert finished["extracted_params"]["기준년월"] == picked["value"]
    assert picked["value"] in finished["final_sql"]


def test_a_verified_query_or_tool_path_never_stops_to_confirm_routing() -> None:
    assert workflow.after_tool_selection({"matched_query_name": "vq"}) == "extract_and_apply_params"
    assert workflow.after_tool_selection({"selected_tool": "tool"}) == "check_tool_params"
    assert workflow.after_tool_selection({}) == "check_domain_choice"


def test_the_confirmed_reading_reaches_the_sql_prompt() -> None:
    captured: list[str] = []

    def capture(prompt: str, **kwargs) -> str:
        captured.append(prompt)
        return "SELECT 1"

    with patch.object(workflow, "_call_llm", side_effect=capture):
        workflow.generate_sql(
            {
                "question": "기타 고객 목록",
                "selected_domain": "",
                "selected_tables": [],
                "table_details": "",
                "clarification_directives": ["'기타'은(는) 기업규모 기준 코드값으로 해석한다."],
            }
        )

    assert captured
    assert "## 사용자가 확정한 해석" in captured[0]
    assert "기업규모 기준 코드값으로 해석한다" in captured[0]

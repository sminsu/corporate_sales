from __future__ import annotations

import json
import unittest

from webapp_compatible_api import adapt_sse_stream, compatible_json_response


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def parse_events(chunks: list[str]) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for chunk in chunks:
        lines = chunk.strip().splitlines()
        event_name = next(line.split(":", 1)[1].strip() for line in lines if line.startswith("event:"))
        data_text = "\n".join(line.split(":", 1)[1].strip() for line in lines if line.startswith("data:"))
        events.append((event_name, json.loads(data_text)))
    return events


class WebAppCompatibleAdapterTest(unittest.TestCase):
    def test_adapt_sse_stream_maps_text2sql_progress_to_webapp_sequence(self) -> None:
        chunks = [
            sse("start", {"message": "질문을 분석 중입니다...", "data": {"session_id": "sess_1", "message_id": 1}}),
            sse(
                "text2sql_progress",
                {
                    "message": "질문에 맞는 업무 도메인을 선택했습니다.",
                    "data": {
                        "text2sql_step": "route_domain",
                        "query": "매출 알려줘",
                        "phase": "domain_routing",
                        "title": "도메인 라우팅",
                        "question_type": "need_sql",
                        "selected_domain": "sales",
                    },
                },
            ),
            sse(
                "text2sql_progress",
                {
                    "message": "DB 조회를 실행했습니다.",
                    "data": {
                        "text2sql_step": "run_query",
                        "query": "매출 알려줘",
                        "phase": "sql_execution",
                        "title": "SQL 실행",
                        "selected_domain": "sales",
                        "selected_tables": ["sales_table"],
                        "sql": "SELECT * FROM sales_table",
                        "columns": ["amount"],
                        "row_count": 3,
                    },
                },
            ),
            sse(
                "text2sql_progress",
                {
                    "message": "조회 결과를 요약 답변으로 정리했습니다.",
                    "data": {
                        "text2sql_step": "generate_answer",
                        "query": "매출 알려줘",
                        "phase": "answer_generation",
                        "title": "답변 생성",
                        "selected_tables": ["sales_table"],
                    },
                },
            ),
            sse(
                "done",
                {
                    "message": "",
                    "data": {
                        "answer": "답변",
                        "documents": [{"title": "테이블: sales_table", "source_url": "", "score": 1.0}],
                        "session_id": "sess_1",
                        "message_id": 1,
                    },
                },
            ),
        ]

        events = parse_events(list(adapt_sse_stream(chunks)))

        self.assertEqual([event for event, _ in events], ["start", "search_plan", "aggregate_review", "response", "done"])
        self.assertEqual(events[1][1]["message"], "질문을 분석하고 SQL 실행 계획을 세우고 있습니다...")
        self.assertEqual(events[1][1]["data"]["operation"], "text2sql_plan")
        self.assertEqual(events[1][1]["data"]["question"], "매출 알려줘")
        self.assertEqual(events[1][1]["data"]["question_type"], "need_sql")
        self.assertEqual(events[1][1]["data"]["selected_domain"], "sales")
        self.assertNotIn("sub_queries", events[1][1]["data"])
        self.assertEqual(events[2][1]["message"], "SQL을 생성/검증하고 조회 결과를 확인하고 있습니다...")
        self.assertEqual(events[2][1]["data"]["operation"], "sql_execution_review")
        self.assertEqual(events[2][1]["data"]["question"], "매출 알려줘")
        self.assertEqual(events[2][1]["data"]["selected_tables"], ["sales_table"])
        self.assertEqual(events[2][1]["data"]["sql"], "SELECT * FROM sales_table")
        self.assertEqual(events[2][1]["data"]["has_sql"], True)
        self.assertEqual(events[2][1]["data"]["columns"], ["amount"])
        self.assertEqual(events[2][1]["data"]["column_count"], 1)
        self.assertEqual(events[2][1]["data"]["row_count"], 3)
        self.assertNotIn("documents_by_sub_query", events[2][1]["data"])
        self.assertEqual(events[3][1]["message"], "조회 결과를 답변으로 정리하고 있습니다...")
        self.assertEqual(events[3][1]["data"]["operation"], "answer_generation")
        self.assertNotIn("compose_documents", events[3][1]["data"])
        self.assertEqual(events[4][1]["data"]["answer"], "답변")

    def test_adapt_sse_stream_keeps_repeated_progress_steps_in_same_webapp_group(self) -> None:
        chunks = [
            sse("start", {"message": "질문을 분석 중입니다...", "data": {"session_id": "sess_1", "message_id": 1}}),
            sse(
                "text2sql_progress",
                {
                    "message": "질문 의도를 분석했습니다.",
                    "data": {
                        "text2sql_step": "classify_question",
                        "query": "매출 알려줘",
                        "phase": "question_analysis",
                        "title": "질문 분석",
                        "question_type": "need_sql",
                    },
                },
            ),
            sse(
                "text2sql_progress",
                {
                    "message": "질문에 맞는 업무 도메인을 선택했습니다.",
                    "data": {
                        "text2sql_step": "route_domain",
                        "query": "매출 알려줘",
                        "phase": "domain_routing",
                        "title": "도메인 라우팅",
                        "selected_domain": "sales",
                    },
                },
            ),
            sse(
                "text2sql_progress",
                {
                    "message": "실행 경로를 선택했습니다.",
                    "data": {
                        "text2sql_step": "select_tool",
                        "query": "매출 알려줘",
                        "phase": "capability_selection",
                        "title": "실행 경로 선택",
                        "matched_query_name": "monthly_sales_summary",
                    },
                },
            ),
            sse(
                "text2sql_progress",
                {
                    "message": "DB 조회를 실행했습니다.",
                    "data": {
                        "text2sql_step": "run_query",
                        "query": "매출 알려줘",
                        "phase": "sql_execution",
                        "title": "SQL 실행",
                        "sql": "SELECT 1",
                        "columns": ["amount"],
                        "row_count": 1,
                    },
                },
            ),
            sse(
                "text2sql_progress",
                {
                    "message": "조회 결과를 요약 답변으로 정리했습니다.",
                    "data": {
                        "text2sql_step": "generate_answer",
                        "query": "매출 알려줘",
                        "phase": "answer_generation",
                        "title": "답변 생성",
                    },
                },
            ),
            sse("done", {"message": "", "data": {"answer": "답변", "question": "매출 알려줘"}}),
        ]

        events = parse_events(list(adapt_sse_stream(chunks)))

        self.assertEqual(
            [event for event, _ in events],
            ["start", "search_plan", "search_plan", "search_plan", "aggregate_review", "response", "done"],
        )
        search_plan_steps = [data["data"]["text2sql_step"] for event, data in events if event == "search_plan"]
        self.assertEqual(search_plan_steps, ["classify_question", "route_domain", "select_tool"])
        search_plan_messages = [data["data"]["progress_message"] for event, data in events if event == "search_plan"]
        self.assertEqual(search_plan_messages, ["질문 의도를 분석했습니다.", "질문에 맞는 업무 도메인을 선택했습니다.", "실행 경로를 선택했습니다."])
        response_payload = next(data for event, data in events if event == "response")
        self.assertEqual(response_payload["data"]["text2sql_step"], "generate_answer")
        self.assertEqual(response_payload["data"]["progress_message"], "조회 결과를 요약 답변으로 정리했습니다.")

    def test_adapt_sse_stream_fills_missing_progress_events_before_done(self) -> None:
        chunks = [
            sse("start", {"message": "질문을 분석 중입니다...", "data": {"session_id": "sess_1", "message_id": 1}}),
            sse(
                "done",
                {
                    "message": "",
                    "data": {
                        "answer": "답변",
                        "question": "매출 알려줘",
                        "documents": [{"title": "테이블: sales_table", "source_url": "", "score": 1.0}],
                        "session_id": "sess_1",
                        "message_id": 1,
                    },
                },
            ),
        ]

        events = parse_events(list(adapt_sse_stream(chunks)))

        self.assertEqual([event for event, _ in events], ["start", "search_plan", "aggregate_review", "response", "done"])
        self.assertEqual(events[1][1]["data"]["question"], "매출 알려줘")
        self.assertEqual(events[2][1]["data"]["row_count"], 0)

    def test_adapt_sse_stream_hides_internal_parameter_required_event(self) -> None:
        chunks = [
            sse("start", {"message": "질문을 분석 중입니다...", "data": {"session_id": "sess_1", "message_id": 1}}),
            sse(
                "parameter_required",
                {
                    "message": "추가 입력이 필요합니다...",
                    "data": {
                        "operation": "parameter_required",
                        "session_id": "sess_1",
                        "message_id": 1,
                        "missing_params": [{"name": "기준년월"}],
                    },
                },
            ),
            sse(
                "done",
                {
                    "message": "",
                    "data": {
                        "answer": "추가 입력이 필요합니다.",
                        "question": "매출 알려줘",
                        "status": "requires_params",
                        "missing_params": [{"name": "기준년월"}],
                        "session_id": "sess_1",
                        "message_id": 1,
                        "insufficient_evidence": True,
                    },
                },
            ),
        ]

        events = parse_events(list(adapt_sse_stream(chunks)))

        self.assertNotIn("parameter_required", [event for event, _ in events])
        self.assertEqual([event for event, _ in events], ["start", "search_plan", "aggregate_review", "response", "done"])
        self.assertEqual(events[3][1]["data"]["operation"], "answer_generation")
        self.assertEqual(events[3][1]["data"]["missing_params"], [{"name": "기준년월"}])
        self.assertEqual(events[-1][1]["data"]["status"], "requires_params")

    def test_adapt_sse_stream_maps_followup_progress_result_to_webapp_sequence(self) -> None:
        chunks = [
            sse("start", {"message": "질문을 분석 중입니다...", "data": {"session_id": "sess_1", "message_id": 2}}),
            sse("progress", {"step": "start", "title": "요청 접수", "message": "이전 결과를 불러왔습니다."}),
            sse("progress", {"step": "followup_route", "title": "후속 의도 판단", "message": "후속 요청을 기존 결과 분석으로 분류했습니다."}),
            sse("progress", {"step": "generate_answer", "title": "답변 생성", "message": "기존 결과를 기반으로 답변을 생성합니다."}),
            sse(
                "result",
                {
                    "answer": "후속 답변",
                    "question": "원 질문",
                    "followup_question": "이상치 찾아줘",
                    "status": "complete",
                    "result_id": "result-1",
                    "source": "후속 분석",
                    "columns": ["amount"],
                    "rows": [[1000]],
                    "session_id": "sess_1",
                    "message_id": 2,
                },
            ),
        ]

        events = parse_events(list(adapt_sse_stream(chunks)))

        self.assertEqual([event for event, _ in events], ["start", "search_plan", "aggregate_review", "response", "done"])
        self.assertEqual(events[1][1]["data"]["question"], "이상치 찾아줘")
        self.assertEqual(events[2][1]["data"]["operation"], "sql_execution_review")
        self.assertEqual(events[3][1]["data"]["operation"], "answer_generation")
        self.assertEqual(events[-1][1]["data"]["answer"], "후속 답변")

    def test_adapt_sse_stream_keeps_followup_new_sql_progress_in_order(self) -> None:
        # Regression: followup progress events must carry `query` so the
        # search_plan gate does not drop the early steps and reorder them
        # behind the aggregate_review/response events emitted later.
        def progress(step: str, message: str) -> str:
            return sse("progress", {"step": step, "title": step, "message": message, "query": "이번달 매출은?"})

        chunks = [
            sse("start", {"message": "질문을 분석 중입니다...", "data": {"session_id": "sess_1", "message_id": 2}}),
            progress("start", "이전 결과와 대화 이력을 불러왔습니다."),
            progress("followup_route", "후속 요청을 새 SQL 실행으로 분류했습니다."),
            progress("classify_question", "질문 의도를 분석했습니다."),
            progress("route_domain", "도메인을 선택했습니다."),
            progress("analyze_question", "테이블을 선별했습니다."),
            progress("generate_sql", "SQL을 생성했습니다."),
            progress("run_query", "DB 조회를 실행했습니다."),
            progress("generate_answer", "답변을 정리했습니다."),
            progress("complete", "최종 결과를 화면에 표시합니다."),
            sse(
                "result",
                {
                    "answer": "후속 답변",
                    "question": "원 질문",
                    "followup_question": "이번달 매출은?",
                    "status": "complete",
                    "columns": ["amount"],
                    "rows": [[1000]],
                    "session_id": "sess_1",
                    "message_id": 2,
                },
            ),
        ]

        order = [event for event, _ in parse_events(list(adapt_sse_stream(chunks)))]

        # search_plan steps must be emitted in real time (before the later
        # phases), never back-filled after aggregate_review/response.
        self.assertEqual(order[0], "start")
        self.assertEqual(order[-1], "done")
        groups = [event for event in order if event in {"search_plan", "aggregate_review", "response"}]
        self.assertEqual(groups, sorted(groups, key=["search_plan", "aggregate_review", "response"].index))
        self.assertGreaterEqual(groups.count("search_plan"), 3)

    def test_compatible_json_response_wraps_success_envelope(self) -> None:
        response = compatible_json_response({"answer": "답변", "rows": [(1,)]})

        self.assertEqual(response["success"], True)
        self.assertEqual(response["data"]["answer"], "답변")
        self.assertEqual(response["data"]["rows"], [[1]])


if __name__ == "__main__":
    unittest.main()

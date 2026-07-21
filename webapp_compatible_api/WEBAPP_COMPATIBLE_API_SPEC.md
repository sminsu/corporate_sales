# WebApp Compatible API 스펙

## 개요

이 문서는 Text2SQL 서버의 `/api/v1` WebApp 호환 API 형식을 정의합니다.

- SSE(Server-Sent Events) 스트리밍 응답 지원
- 일반 JSON 응답(비스트리밍) 지원
- 세션 ID, 메시지 ID, 대화 ID 반환
- 이벤트 흐름은 WebApp API 형식을 따르되, 이벤트 내부 `data`는 Text2SQL 실행 의미에 맞춰 제공

## 1. 채팅 메시지 전송(SSE 스트리밍 방식)

엔드포인트:

```text
POST /api/v1/agent/{agent_name}/query/stream
```

### 요청 헤더

| 헤더 | 타입 | 필수 | 설명 |
|---|---:|---:|---|
| Content-Type | application/json | 예 | JSON 본문 형식 |
| X-Session-ID | string | 아니오 | 기존 세션 ID |
| X-User-ID | string | 예 | 사용자 ID |
| X-Agent-Name | string | 예 | 에이전트 이름 |

### 요청 본문

```json
{
  "query": "질문 내용",
  "agent_name": "manual",
  "top_k": 10,
  "session_id": "session_123",
  "result_id": "previous_result_id",
  "conversation_history": [
    {
      "role": "user",
      "content": "이전 질문"
    },
    {
      "role": "assistant",
      "content": "이전 답변"
    }
  ]
}
```

`result_id`는 선택 필드입니다. 이전 조회 결과를 기반으로 후속 질문을 처리할 때만 전달합니다.

### 응답

Content-Type:

```text
text/event-stream
```

응답 헤더:

```text
Cache-Control: no-cache
Connection: keep-alive
X-Stream-Request-ID: <HTTP 스트림 상관관계 ID>
X-Stream-Message-ID: <메시지/실행 로그 상관관계 ID>
X-App-Release: <배포 이미지 버전>
```

스트림이 중간에 끊기면 위 세 헤더를 서버의 ECS/CloudWatch 로그와 대조합니다.
`X-Stream-Request-ID`는 ASGI 전송 경계 로그, `X-Stream-Message-ID`는 agent 실행 로그를
찾는 데 사용합니다.

### SSE 이벤트 순서

```text
start -> search_plan -> aggregate_review -> response -> done
```

이벤트 이름은 기존 WebApp 흐름과 맞추지만, `search_plan`, `aggregate_review`, `response`의 내부 `data.operation`은 Text2SQL 기준입니다. 오류가 발생하면 `error` 이벤트가 발생할 수 있습니다. 긴 연결에서는 `heartbeat` 이벤트를 추가로 사용할 수 있습니다.

### start 이벤트

```text
event: start
data: {
  "message": "질문을 분석 중입니다...",
  "data": {
    "session_id": "sess_abc123",
    "message_id": 12345,
    "conversation_id": 100
  }
}
```

### search_plan 이벤트

```text
event: search_plan
data: {
  "message": "질문을 분석하고 SQL 실행 계획을 세우고 있습니다...",
  "data": {
    "operation": "text2sql_plan",
    "question": "질문 내용",
    "text2sql_step": "route_domain",
    "phase": "domain_routing",
    "title": "도메인 라우팅",
    "question_type": "need_sql",
    "selected_domain": "sales",
    "selected_tool": "",
    "matched_query_name": "",
    "selected_tables": [],
    "param_stage": "",
    "missing_params": []
  }
}
```

필드 의미:

| 필드 | 설명 |
|---|---|
| operation | 이 이벤트의 Text2SQL 의미입니다. `text2sql_plan`으로 고정됩니다. |
| question | 사용자 질문입니다. |
| text2sql_step | 내부 LangGraph 노드 이름입니다. |
| phase | UI가 진행 상태를 묶어서 표시할 수 있는 단계 그룹입니다. |
| title | 진행 상태 제목입니다. |
| question_type | 질문 분류 결과입니다. 예: `need_sql`, `direct_answer`, `out_of_scope`. |
| selected_domain | 선택된 업무 도메인입니다. |
| selected_tool | 전용 Tool을 쓰는 경우 Tool 이름입니다. |
| matched_query_name | 검증 쿼리와 매칭된 경우 쿼리 이름입니다. |
| selected_tables | SQL 생성 경로에서 현재까지 선택된 테이블 목록입니다. |
| param_stage | 추가 파라미터 필요 여부를 나타내는 내부 상태입니다. |
| missing_params | 추가 입력이 필요한 파라미터 목록입니다. |

### aggregate_review 이벤트

```text
event: aggregate_review
data: {
  "message": "SQL을 생성/검증하고 조회 결과를 확인하고 있습니다...",
  "data": {
    "operation": "sql_execution_review",
    "question": "질문 내용",
    "text2sql_step": "run_query",
    "phase": "sql_execution",
    "title": "SQL 실행",
    "source": "SQL 생성",
    "selected_domain": "sales",
    "selected_tool": "",
    "matched_query_name": "",
    "selected_tables": ["sales_table"],
    "sql": "SELECT ...",
    "has_sql": true,
    "columns": ["amount"],
    "column_count": 1,
    "row_count": 3,
    "param_stage": "",
    "missing_params": [],
    "error": ""
  }
}
```

필드 의미:

| 필드 | 설명 |
|---|---|
| operation | 이 이벤트의 Text2SQL 의미입니다. `sql_execution_review`로 고정됩니다. |
| question | 사용자 질문입니다. |
| text2sql_step | 내부 LangGraph 노드 이름입니다. |
| phase | SQL 생성, 검증, 실행 등 진행 단계 그룹입니다. |
| title | 진행 상태 제목입니다. |
| source | 실행 경로입니다. 예: Tool, 검증 쿼리, SQL 생성, 직접 답변. |
| selected_domain | 선택된 업무 도메인입니다. |
| selected_tool | 전용 Tool 이름입니다. 없으면 빈 문자열입니다. |
| matched_query_name | 검증 쿼리 이름입니다. 없으면 빈 문자열입니다. |
| selected_tables | 사용된 테이블 목록입니다. |
| sql | 생성 또는 실행된 SQL입니다. 아직 생성 전이면 빈 문자열입니다. |
| has_sql | SQL 존재 여부입니다. |
| columns | 조회 결과 컬럼 목록입니다. |
| column_count | 조회 결과 컬럼 수입니다. |
| row_count | 조회 결과 행 수입니다. |
| param_stage | 추가 파라미터 필요 여부를 나타내는 내부 상태입니다. |
| missing_params | 추가 입력이 필요한 파라미터 목록입니다. |
| error | SQL 생성, 검증, 실행 중 발생한 오류 메시지입니다. |

### response 이벤트

```text
event: response
data: {
  "message": "조회 결과를 답변으로 정리하고 있습니다...",
  "data": {
    "operation": "answer_generation",
    "text2sql_step": "generate_answer",
    "phase": "answer_generation",
    "title": "답변 생성",
    "status": "complete",
    "result_id": "result-id",
    "source": "SQL 생성",
    "has_sql": true,
    "row_count": 3,
    "column_count": 1,
    "answer_ready": true,
    "missing_params": [],
    "suggestions": []
  }
}
```

필드 의미:

| 필드 | 설명 |
|---|---|
| operation | 이 이벤트의 Text2SQL 의미입니다. `answer_generation`으로 고정됩니다. |
| text2sql_step | 내부 LangGraph 노드 이름입니다. |
| phase | 답변 생성 단계 그룹입니다. |
| title | 진행 상태 제목입니다. |
| status | 결과 상태입니다. 예: `complete`, `requires_params`. |
| result_id | 결과 다운로드/후속 분석에 사용할 결과 ID입니다. |
| source | 최종 실행 경로입니다. |
| has_sql | 최종 결과에 SQL이 있는지 여부입니다. |
| row_count | 최종 조회 결과 행 수입니다. |
| column_count | 최종 조회 결과 컬럼 수입니다. |
| answer_ready | 답변이 생성되었거나 추가 입력 안내가 준비되었는지 여부입니다. |
| missing_params | 추가 입력이 필요한 파라미터 목록입니다. |
| suggestions | 후속 질문 추천 목록입니다. |

### done 이벤트

```text
event: done
data: {
  "message": "",
  "data": {
    "answer": "최종 답변 내용",
    "session_id": "sess_abc123",
    "message_id": 12345,
    "conversation_id": 100,
    "images": [],
    "insufficient_evidence": false,
    "status": "complete",
    "result_id": "result-id",
    "source": "SQL 생성",
    "selected_tables": ["sales_table"],
    "sql": "SELECT ...",
    "columns": ["amount"],
    "rows": [[1000]],
    "result_meta": {},
    "suggestions": []
  }
}
```

`documents` 필드는 기존 WebApp 호환을 위해 남아 있을 수 있지만, 이 서비스는 RAG 검색을 하지 않으므로 UI는 `selected_tables`, `sql`, `columns`, `rows`, `result_meta`를 우선 사용합니다.

추가 입력이 필요한 경우:

```json
{
  "status": "requires_params",
  "missing_params": [
    {
      "name": "기준년월",
      "label": "기준년월"
    }
  ],
  "continuation": {},
  "insufficient_evidence": true
}
```

이 경우 별도의 `parameter_required` 이벤트를 외부로 내보내지 않고, `done.data.status`로 판단합니다.

### error 이벤트

```text
event: error
data: {
  "message": "오류가 발생했습니다.",
  "data": {
    "error": "상세 오류 메시지",
    "message_id": 12345
  }
}
```

## 2. 채팅 메시지 전송(비스트리밍/일반)

엔드포인트:

```text
POST /api/v1/agent/{agent_name}/query
```

### 요청 헤더

| 헤더 | 타입 | 필수 | 설명 |
|---|---:|---:|---|
| Content-Type | application/json | 예 | JSON 본문 형식 |
| X-Session-ID | string | 아니오 | 기존 세션 ID |
| X-User-ID | string | 예 | 사용자 ID |
| X-Agent-Name | string | 예 | 에이전트 이름 |

### 요청 본문

```json
{
  "query": "질문 내용",
  "agent_name": "manual",
  "top_k": 10,
  "session_id": "session_123",
  "result_id": null,
  "conversation_history": []
}
```

### 응답

Content-Type:

```text
application/json
```

```json
{
  "success": true,
  "data": {
    "answer": "답변 내용",
    "session_id": "sess_abc123",
    "message_id": 12345,
    "conversation_id": 100,
    "images": [],
    "insufficient_evidence": false,
    "status": "complete",
    "result_id": "result-id",
    "source": "SQL 생성",
    "selected_tables": ["sales_table"],
    "sql": "SELECT ...",
    "columns": ["amount"],
    "rows": [[1000]],
    "result_meta": {},
    "suggestions": []
  }
}
```

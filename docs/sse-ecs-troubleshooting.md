# ECS SSE 스트림 중단 진단

브라우저의 `net::ERR_INCOMPLETE_CHUNKED_ENCODING 200 (OK)`는 HTTP 응답 헤더는 정상적으로
전달됐지만 SSE 응답 본문이 종료 프레임 전에 끊겼다는 뜻이다. 200 상태만으로 애플리케이션이
정상 완료됐다고 판단하면 안 된다.

## 배포 버전 식별

Docker 이미지 빌드 시 커밋 또는 이미지 태그를 `APP_RELEASE`로 넣는다.

```bash
docker build \
  --build-arg APP_RELEASE="$(git rev-parse --short HEAD)" \
  -t corporate-sales-t2s:<tag> .
```

ECS Task Definition이 태그 대신 image digest를 사용한다면 digest와 커밋의 매핑도 배포 기록에
남긴다. 서비스 시작 로그의 `app_release`, 응답 헤더 `X-App-Release`, 브라우저 콘솔의
`appRelease` 값은 서로 같아야 한다.

## 실패 요청 식별값

브라우저 개발자 도구의 실패한 `/query/stream` 응답 헤더에서 다음 값을 복사한다.

- `X-Stream-Request-ID`: HTTP 스트림 한 건을 식별한다.
- `X-Stream-Message-ID`: 애플리케이션 메시지와 실행 로그를 연결한다.
- `X-App-Release`: 실제 요청을 처리한 이미지 버전을 식별한다.

프런트엔드도 스트림이 중간에 끊기면 위 값, 마지막 SSE 이벤트, 수신 바이트 수를
`[SSE stream interrupted]` 콘솔 로그로 남긴다. 질문이나 SQL 본문은 이 로그에 남기지 않는다.
서버 진단 이벤트는 ECS stdout/stderr에 한 줄 JSON으로 출력되므로 CloudWatch Logs Insights가
각 필드를 자동으로 추출할 수 있다.

## CloudWatch Logs Insights 조회

실패 화면에 표시된 ID를 아래 쿼리의 값에 넣는다. JSON 로그 필드가 자동 추출되지 않는
로그 그룹에서는 두 번째 `filter`처럼 원문 검색을 사용한다.

```text
fields @timestamp, event, stream_request_id, message_id, app_release,
       container_id, status_code, body_bytes, body_frames, elapsed_ms,
       stream_stage, last_sse_event, terminal_sent, error_type, error_message
| filter stream_request_id = "<X-Stream-Request-ID>"
    or message_id = "<X-Stream-Message-ID>"
    or @message like /<X-Stream-Request-ID>/
| sort @timestamp asc
| limit 200
```

ID를 얻지 못했다면 시간 범위를 실패 전후 5분으로 좁혀 다음 이벤트를 조회한다.

```text
fields @timestamp, @message
| filter @message like /stream_http_|sse_stream_|stream_query_|stream_execution_log_failed/
| sort @timestamp desc
| limit 500
```

## 이벤트 판정표

| 마지막으로 확인되는 로그 | 판정 | 다음 확인 대상 |
|---|---|---|
| `stream_http_response_completed` | 애플리케이션은 ASGI 종료 프레임까지 전송함 | ALB idle timeout, 프록시, 브라우저/VPN 연결 |
| `stream_http_response_aborted` + `stream_http_app_exception` | 애플리케이션/라이브러리 예외로 중단 | 같은 ID의 `error_type`, `error_message`, `exception_trace` |
| `stream_execution_log_failed` 후 `done`/`response_completed` | 공통 로거는 실패했지만 응답은 보호됨 | 로거 설정은 별도 수정; 스트림 원인은 아님 |
| `sse_stream_iteration_failed` | SSE generator/adapter가 예외를 냄 | `last_sse_event`, traceback, `stream_stage` |
| `stream_http_client_disconnected` | Uvicorn이 클라이언트 disconnect를 수신함 | ALB access log와 사용자 네트워크 |
| `stream_http_request_started` 뒤 종료 로그가 없고 새 `service_started` 발생 | Task가 비정상 종료/교체됐을 가능성이 큼 | ECS stopped reason, exit code, OOM, 배포 이벤트 |
| `stream_done_serialized`의 `chunk_bytes`가 매우 큼 | 최종 단일 SSE 이벤트가 과대함 | 세션 messages/rows/history 축소 또는 결과 별도 조회 |

## ECS와 ALB에서 함께 확인할 항목

1. ECS 서비스 이벤트에서 요청 시각의 Task 교체, health check 실패, deployment 진행 여부를 본다.
2. 중지된 Task의 `stoppedReason`, 컨테이너 `exitCode`를 확인한다. `137`이면 메모리 강제 종료
   가능성이 높다.
3. Container Insights를 사용하는 경우 같은 시각의 Memory/CPU 최대값을 본다.
4. ALB access log에서 동일 시각과 경로의 `elb_status_code`, `target_status_code`,
   `request_processing_time`, `target_processing_time`, `response_processing_time`을 비교한다.
5. 실패 시간이 항상 같은 초(예: 60초)에 발생하면 ALB 또는 중간 프록시 idle timeout을 확인한다.
   이 서비스는 처리 중 2.5초마다 heartbeat를 보내므로, heartbeat가 ALB까지 도달하는지도 함께 본다.

## 정상 판정 기준

한 요청에서 아래 순서가 모두 나타나면 애플리케이션 스트림은 정상 종료된 것이다.

```text
stream_http_request_started
stream_http_response_started
stream_http_first_body_sent
sse_event_sent (done 또는 result)
stream_http_response_completed
```

`stream_http_response_completed`가 있는데 브라우저에는 `reader_failed`가 남으면 수정 대상은
FastAPI generator가 아니라 ALB 이후 경로다. 반대로 `response_aborted`가 있으면 같은 요청 ID의
애플리케이션 traceback부터 수정한다.

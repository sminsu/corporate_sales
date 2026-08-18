from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import patch

import pytest

import web_service
from text2sql_agent import config, pii, workflow


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class _Client:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    def post(self, path: str, *, json: dict):
        self.calls.append((path, json))
        return _Response(self.payload)


def test_pii_client_uses_service_name_header_from_example_contract() -> None:
    sentinel = object()
    with (
        patch.object(config, "PII_BASE_URL", "http://10.95.21.15:18240"),
        patch.object(config, "PII_AGENT_NAME", "ecs-corporate-sales"),
        patch.object(pii, "_CLIENT", None),
        patch.object(pii.httpx, "Client", return_value=sentinel) as client,
    ):
        assert pii._get_client() is sentinel

    assert client.call_args.kwargs["base_url"] == "http://10.95.21.15:18240"
    assert client.call_args.kwargs["headers"] == {"X-Agnet-Name": "ecs-corporate-sales"}
    assert client.call_args.kwargs["timeout"] == 60


def test_mask_pii_text_calls_api_and_returns_masked_text() -> None:
    client = _Client(
        {
            "detected": True,
            "mask_applied": True,
            "text": "내 전화번호는 [계좌번호]인데 내가 대표인 기업을 찾아줘.",
            "items": [],
            "action": "Blocking",
            "source": "ai",
        }
    )
    with (
        patch.object(config, "PII_MASKING_ENABLED", True),
        patch.object(config, "PII_ENDPOINT", "/pii"),
        patch.object(pii, "_get_client", return_value=client),
    ):
        masked = pii.mask_pii_text("내 전화번호는 010-9904-0959 인데 내가 대표인 기업 찾아줘.")

    assert masked == "내 전화번호는 [계좌번호]인데 내가 대표인 기업을 찾아줘."
    assert client.calls == [
        (
            "/pii",
            {
                "text": "내 전화번호는 010-9904-0959 인데 내가 대표인 기업 찾아줘.",
                "mask": True,
            },
        )
    ]


def test_nested_storage_masking_preserves_numbers_and_structure() -> None:
    value = {
        "question": "전화번호는 010-9904-0959",
        "rows": [("홍길동", 100_000_000)],
    }
    masked_strings = ["전화번호는 [전화번호]", "[이름]"]
    with (
        patch.object(config, "PII_MASKING_ENABLED", True),
        patch.object(pii, "mask_pii_text", return_value=json.dumps(masked_strings, ensure_ascii=False)),
    ):
        masked = pii.mask_pii_for_storage(value)

    assert masked == {
        "question": "전화번호는 [전화번호]",
        "rows": [("[이름]", 100_000_000)],
    }


def test_pii_api_failure_never_returns_raw_text() -> None:
    class BrokenClient:
        def post(self, *_args, **_kwargs):
            raise RuntimeError("unavailable")

    with (
        patch.object(config, "PII_MASKING_ENABLED", True),
        patch.object(pii, "_get_client", return_value=BrokenClient()),
        pytest.raises(pii.PiiMaskingError),
    ):
        pii.mask_pii_text("010-9904-0959")


def test_session_message_persists_only_masked_copy() -> None:
    session = {"id": "session-1", "messages": []}

    def mask_message(message: dict) -> dict:
        masked = dict(message)
        masked["content"] = "전화번호는 [전화번호]"
        masked["text"] = masked["content"]
        return masked

    with (
        patch.object(web_service.agent, "mask_pii_for_storage", side_effect=mask_message),
        patch.object(web_service._SESSION_STORE, "append_message") as append_message,
    ):
        web_service._append_message(session, "user", "전화번호는 010-9904-0959")

    stored = append_message.call_args.args[1]
    assert stored["content"] == "전화번호는 [전화번호]"
    assert "010-9904-0959" not in json.dumps(stored, ensure_ascii=False)


def test_session_message_uses_redaction_when_masking_fails() -> None:
    session = {"id": "session-1", "messages": []}
    with (
        patch.object(
            web_service.agent,
            "mask_pii_for_storage",
            side_effect=pii.PiiMaskingError("unavailable"),
        ),
        patch.object(web_service._SESSION_STORE, "append_message") as append_message,
    ):
        web_service._append_message(session, "user", "전화번호는 010-9904-0959")

    stored = append_message.call_args.args[1]
    assert stored["content"] == pii.PII_STORAGE_REDACTION
    assert "010-9904-0959" not in json.dumps(stored, ensure_ascii=False)


def test_saved_result_uses_masked_copy_without_changing_response() -> None:
    phone = "010-9904-0959"
    result = workflow._new_initial_state(f"전화번호 {phone}로 기업을 찾아줘")
    result.update(
        {
            "answer": f"{phone}와 연결된 기업입니다.",
            "query_columns": ["대표자전화번호"],
            "query_rows": [(phone,)],
            "safety_action": "ALLOW",
        }
    )
    session = {
        "id": "session-result",
        "user_id": "user-1",
        "messages": [],
        "title": "새 대화",
    }

    def fake_mask(value):
        def replace(item):
            if isinstance(item, str):
                return item.replace(phone, "[전화번호]")
            if isinstance(item, dict):
                return {key: replace(child) for key, child in item.items()}
            if isinstance(item, list):
                return [replace(child) for child in item]
            if isinstance(item, tuple):
                return tuple(replace(child) for child in item)
            return item

        return replace(deepcopy(value))

    with (
        patch.object(web_service.agent, "mask_pii_for_storage", side_effect=fake_mask),
        patch.object(web_service.agent, "check_content_safety", return_value={
            "action": "ALLOW",
            "category": "NONE",
            "reason_code": "SAFE",
            "direction": "OUTPUT",
        }),
        patch.object(web_service, "_suggest_followups", return_value=[]),
        patch.object(web_service._SESSION_STORE, "save_result") as save_result,
        patch.object(web_service._SESSION_STORE, "update_session_state"),
    ):
        payload = web_service._result_payload(result, session, message_id=1, top_k=10)

    stored = save_result.call_args.args[2]
    assert phone not in json.dumps(stored, ensure_ascii=False)
    assert "[전화번호]" in json.dumps(stored, ensure_ascii=False)
    assert payload["answer"] == f"{phone}와 연결된 기업입니다."

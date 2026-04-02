from types import SimpleNamespace

from nanobot.providers.anthropic_provider import AnthropicProvider
from nanobot.providers.openai_compat_provider import OpenAICompatProvider


def _fake_response(
    *,
    status_code: int,
    headers: dict[str, str] | None = None,
    text: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status_code,
        headers=headers or {},
        text=text,
    )


def test_openai_handle_error_extracts_structured_metadata() -> None:
    class FakeStatusError(Exception):
        pass

    err = FakeStatusError("boom")
    err.status_code = 409
    err.response = _fake_response(
        status_code=409,
        headers={"retry-after-ms": "250", "x-should-retry": "false"},
        text='{"error":"conflict"}',
    )
    err.body = {"error": "conflict"}

    response = OpenAICompatProvider._handle_error(err)

    assert response.finish_reason == "error"
    assert response.error_status_code == 409
    assert response.error_retry_after_s == 0.25
    assert response.error_should_retry is False


def test_openai_handle_error_marks_timeout_kind() -> None:
    class FakeTimeoutError(Exception):
        pass

    response = OpenAICompatProvider._handle_error(FakeTimeoutError("timeout"))

    assert response.finish_reason == "error"
    assert response.error_kind == "timeout"


def test_anthropic_error_response_extracts_structured_metadata() -> None:
    class FakeStatusError(Exception):
        pass

    err = FakeStatusError("boom")
    err.status_code = 408
    err.response = _fake_response(
        status_code=408,
        headers={"retry-after": "1.5", "x-should-retry": "true"},
    )

    response = AnthropicProvider._error_response(err)

    assert response.finish_reason == "error"
    assert response.error_status_code == 408
    assert response.error_retry_after_s == 1.5
    assert response.error_should_retry is True


def test_anthropic_error_response_marks_connection_kind() -> None:
    class FakeConnectionError(Exception):
        pass

    response = AnthropicProvider._error_response(FakeConnectionError("connection"))

    assert response.finish_reason == "error"
    assert response.error_kind == "connection"

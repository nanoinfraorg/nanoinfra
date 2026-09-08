"""`server_error` is the code OpenAI sends, and the retry loop has to know it.

A mid-stream `response.failed` becomes `RuntimeError("Response failed: {...}")` carrying the raw
payload (`openai_responses/parsing.py:522`), so the only thing the retry loop can classify is that
text. `fallback_provider.py` has listed the underscored token from the start; the retry path had
only the spaced one, which meant the two halves of the same policy disagreed about the same error.
"""

from __future__ import annotations

from nanoinfra.providers.base import LLMProvider, LLMResponse
from nanoinfra.providers.fallback_provider import FallbackProvider


def _failed_response(detail: str) -> LLMResponse:
    """The shape `_handle_error` produces for a mid-stream `response.failed`."""
    return LLMResponse(content=f"Error calling LLM: Response failed: {detail}", finish_reason="error")


def test_underscored_server_error_code_is_transient() -> None:
    response = _failed_response(
        "{'code': 'server_error', 'message': 'The model produced invalid content.'}"
    )
    assert LLMProvider.is_transient_response(response) is True


def test_spaced_server_error_text_stays_transient() -> None:
    assert LLMProvider.is_transient_response(_failed_response("internal server error")) is True


def test_retry_and_fallback_paths_agree_on_the_code() -> None:
    """The two policies read the same error; neither may call it terminal on its own."""
    response = _failed_response("{'code': 'server_error'}")
    assert LLMProvider.is_transient_response(response) is True
    assert FallbackProvider._should_fallback(response) is True

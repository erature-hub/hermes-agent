"""Regression: a local model that is DOWNLOADED BUT NOT LOADED is not a
format error.

A local inference server (llama.cpp / LM Studio / Unsloth Studio) that has
the model on disk but has not loaded it rejects the request with a body that
names the fix:

    "No model loaded. Call POST /inference/load first. Or enable Model
     auto-switch (Settings > API) to load a requested model automatically."
    "The model '<id>' is downloaded but not loaded, and 'Switch model by
     request' is off, so this server can only serve the loaded model."

Before this fix these fell through to the generic 400 catch-all and became
``format_error``, which is NON-RETRYABLE and aborts the turn. The user is
then told their CONVERSATION is malformed ("<provider> couldn't accept how
this conversation is structured. Switch provider, or start a new chat") —
which is false, and it bricks the session: the same request succeeds the
moment the model is loaded, but nothing ever retries it.

The request shape is FINE. The server's state is wrong. So this must be its
own reason, retryable, and must not fall back to another provider (a
different provider would not have the local model either).
"""

from types import SimpleNamespace

import pytest

from agent.error_classifier import (
    FailoverReason,
    classify_api_error,
)


class MockAPIError(Exception):
    """Simulates an OpenAI SDK APIStatusError."""

    def __init__(self, message, status_code=None, body=None, headers=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body or {}
        self.response = SimpleNamespace(headers=headers or {})


# The exact body llama.cpp / LM Studio / Unsloth Studio returned.
NO_MODEL_LOADED_400 = {
    "error": {
        "message": (
            "No model loaded. Call POST /inference/load first. Or enable "
            "Model auto-switch (Settings > API) to load a requested model "
            "automatically."
        ),
        "type": "invalid_request_error",
        "param": None,
        "code": None,
    }
}

NOT_LOADED_404 = {
    "error": {
        "message": (
            "The model 'mudler/Ornith-1.5-35B-A3B-APEX-MTP-GGUF' is downloaded "
            "but not loaded, and 'Switch model by request' is off, so this server "
            "can only serve the loaded model."
        ),
        "type": "not_found_error",
        "param": "model",
        "code": "model_not_found",
    }
}


class TestModelNotLoaded:
    """The 400 'No model loaded' must not be classified as a format error."""

    def test_400_no_model_loaded_is_model_not_loaded(self):
        err = MockAPIError(
            "Error code: 400 - {'error': {'message': 'No model loaded. Call "
            "POST /inference/load first. Or enable Model auto-switch (Settings "
            "> API) to load a requested model automatically.', 'type': "
            "'invalid_request_error', 'param': None, 'code': None}}",
            status_code=400,
            body=NO_MODEL_LOADED_400,
        )
        verdict = classify_api_error(err, provider="custom", model="unsloth/Ornith-1.5-122B-GGUF")
        assert verdict.reason is FailoverReason.model_not_loaded, verdict.reason

    def test_model_not_loaded_is_retryable(self):
        """The whole point: retrying after the host loads the model succeeds."""
        err = MockAPIError(
            "Error code: 400 - No model loaded. Call POST /inference/load first.",
            status_code=400,
            body=NO_MODEL_LOADED_400,
        )
        verdict = classify_api_error(err, provider="custom", model="unsloth/Ornith-1.5-122B-GGUF")
        assert verdict.retryable is True, "a loadable model must be retried, not aborted"

    def test_model_not_loaded_does_not_fall_back(self):
        """A different provider would not have the local model either."""
        err = MockAPIError(
            "Error code: 400 - No model loaded.",
            status_code=400,
            body=NO_MODEL_LOADED_400,
        )
        verdict = classify_api_error(err, provider="custom", model="unsloth/Ornith-1.5-122B-GGUF")
        assert verdict.should_fallback is False, "falling back cannot fix an unloaded local model"

    def test_404_downloaded_but_not_loaded_is_model_not_loaded(self):
        """The same server state can arrive as a 404 with a known slug."""
        err = MockAPIError(
            "Error code: 404 - The model 'mudler/Ornith-1.5-35B-A3B-APEX-MTP-GGUF' "
            "is downloaded but not loaded, and 'Switch model by request' is off, "
            "so this server can only serve the loaded model.",
            status_code=404,
            body=NOT_LOADED_404,
        )
        verdict = classify_api_error(err, provider="custom", model="mudler/Ornith-1.5-35B-A3B-APEX-MTP-GGUF")
        assert verdict.reason is FailoverReason.model_not_loaded, verdict.reason
        assert verdict.retryable is True


class TestFormatErrorUnaffected:
    """A genuinely malformed request must STILL be a format error."""

    def test_real_format_error_stays_format_error(self):
        err = MockAPIError(
            "Error code: 400 - {'error': {'message': \"Invalid 'messages[1].content': "
            "expected a string, but got an array instead.\"}}",
            status_code=400,
            body={"error": {"message": "Invalid 'messages[1].content': expected a string, but got an array instead."}},
        )
        verdict = classify_api_error(err, provider="custom", model="unsloth/Ornith-1.5-122B-GGUF")
        assert verdict.reason is FailoverReason.format_error, verdict.reason

    def test_unknown_model_stays_model_not_found(self):
        """'No such model' is a DEAD slug, not an unloaded one."""
        err = MockAPIError(
            "Error code: 404 - {'error': {'message': 'The model `gpt-9-turbo` does "
            "not exist or you do not have access to it.'}}",
            status_code=404,
            body={"error": {"message": "The model `gpt-9-turbo` does not exist or you do not have access to it."}},
        )
        verdict = classify_api_error(err, provider="custom", model="gpt-9-turbo")
        assert verdict.reason is FailoverReason.model_not_found, verdict.reason

    def test_unrelated_400_still_format_error(self):
        err = MockAPIError(
            "Error code: 400 - {'error': {'message': 'Unsupported parameter: "
            "'temperature' is not supported with this model.'}}",
            status_code=400,
            body={"error": {"message": "Unsupported parameter: 'temperature' is not supported with this model."}},
        )
        verdict = classify_api_error(err, provider="custom", model="unsloth/Ornith-1.5-122B-GGUF")
        assert verdict.reason is FailoverReason.format_error, verdict.reason

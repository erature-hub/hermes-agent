"""End-to-end: the surface the DESKTOP receives for an unloaded local model.

`build_error_surface_from_result` turns the backend's failed result into the
surface the desktop renders. The reason is stamped verbatim as the code, so
the desktop's ERROR_CODE_KEYS list must contain it, and the retryable flag
must come from the classifier's own verdict (not the reason-set fallback).
"""

from types import SimpleNamespace

from agent.error_classifier import FailoverReason, classify_api_error
from agent.error_surface import build_error_surface_from_result


class MockAPIError(Exception):
    def __init__(self, message, status_code=None, body=None, headers=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body or {}
        self.response = SimpleNamespace(headers=headers or {})


BODY = {
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


def test_unloaded_model_surfaces_as_retryable_model_not_loaded():
    err = MockAPIError(
        "Error code: 400 - No model loaded. Call POST /inference/load first.",
        status_code=400,
        body=BODY,
    )
    classified = classify_api_error(
        err, provider="custom", model="unsloth/Ornith-1.5-122B-GGUF"
    )
    assert classified.reason is FailoverReason.model_not_loaded

    result = {
        "failed": True,
        "error": "No model loaded. Call POST /inference/load first.",
        "failure_reason": classified.reason.value,
        "failure_retryable": bool(classified.retryable),
    }
    surface = build_error_surface_from_result(
        result, provider="custom", model="unsloth/Ornith-1.5-122B-GGUF"
    )
    assert surface is not None
    assert surface["code"] == "model_not_loaded", surface
    # The desktop must offer RETRY, not "switch provider / start a new chat".
    assert surface["retryable"] is True, surface

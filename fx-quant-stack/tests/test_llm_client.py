"""Tests for the local-first LLM client (offline-safe, localhost-guarded)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from fxstack.llm.client import (
    LLMClient,
    LLMUnavailable,
    NullLLMClient,
    OllamaClient,
    OpenAICompatClient,
    build_llm_client,
    is_local_url,
)


class _Toy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    value: float


class _ScriptedClient(LLMClient):
    """Returns canned completions to exercise parse/retry without a server."""

    backend = "scripted"

    def __init__(self, replies: list[str]) -> None:
        super().__init__(model="scripted", base_url="", max_retries=len(replies))
        self._replies = list(replies)
        self.calls = 0

    def health(self):  # pragma: no cover - not used here
        raise NotImplementedError

    def _complete_json(self, *, system, prompt, seed, temperature):
        reply = self._replies[min(self.calls, len(self._replies) - 1)]
        self.calls += 1
        return reply


def test_is_local_url():
    assert is_local_url("http://127.0.0.1:11434")
    assert is_local_url("http://localhost:8000")
    assert is_local_url("http://[::1]:1234")
    assert not is_local_url("http://example.com:11434")
    assert not is_local_url("https://10.0.0.5:8000")


def test_null_client_is_unavailable_and_raises():
    client = NullLLMClient(model="x")
    health = client.health()
    assert health.available is False
    assert client.backend == "null"
    with pytest.raises(LLMUnavailable):
        client.generate_structured(schema=_Toy, prompt="hi")


def test_build_defaults_to_null():
    class _S:
        llm_backend = "null"
        llm_model = "m"
        llm_base_url = "http://127.0.0.1:11434"
        llm_timeout_s = 5.0
        llm_max_retries = 1
        llm_api_key = ""
        agent_allow_remote_llm = False

    client = build_llm_client(_S())
    assert isinstance(client, NullLLMClient)


def test_remote_url_blocked_without_override():
    with pytest.raises(LLMUnavailable):
        OllamaClient(model="m", base_url="http://evil.example.com:11434", allow_remote=False)
    with pytest.raises(LLMUnavailable):
        OpenAICompatClient(model="m", base_url="http://10.1.2.3:8000", allow_remote=False)


def test_remote_url_allowed_with_override():
    # Construction must succeed when explicitly permitted (no network call here).
    client = OllamaClient(model="m", base_url="http://gpu-host:11434", allow_remote=True)
    assert client.base_url == "http://gpu-host:11434"


def test_build_remote_backend_without_override_falls_back_to_null():
    class _S:
        llm_backend = "ollama"
        llm_model = "m"
        llm_base_url = "http://remote.example.com:11434"
        llm_timeout_s = 5.0
        llm_max_retries = 1
        llm_api_key = ""
        agent_allow_remote_llm = False

    client = build_llm_client(_S())
    assert isinstance(client, NullLLMClient)


def test_structured_generation_recovers_from_bad_then_good_json():
    client = _ScriptedClient([
        "not json at all",
        '```json\n{"name": "a", "value": 1.5}\n```',
    ])
    out = client.generate_structured(schema=_Toy, prompt="give me a toy")
    assert out.name == "a"
    assert out.value == 1.5
    assert client.calls == 2


def test_structured_generation_extracts_embedded_object():
    client = _ScriptedClient(['Sure! Here you go: {"name": "b", "value": 2} -- enjoy'])
    out = client.generate_structured(schema=_Toy, prompt="x")
    assert out.name == "b"
    assert out.value == 2.0


def test_structured_generation_gives_up_to_unavailable():
    client = _ScriptedClient(["nope", "still nope", "nada"])
    with pytest.raises(LLMUnavailable):
        client.generate_structured(schema=_Toy, prompt="x")

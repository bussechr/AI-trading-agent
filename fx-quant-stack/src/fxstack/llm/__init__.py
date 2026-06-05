# AGENT: ROLE: Local-first LLM client used by the self-improvement proposer ("LLM proposes").
# AGENT: ENTRYPOINT: `build_llm_client(settings)` -> LLMClient.
# AGENT: PRIMARY INPUTS: FXSTACK_LLM_* settings (backend/base_url/model/seed/temperature).
# AGENT: PRIMARY OUTPUTS: schema-validated pydantic objects from local Ollama / vLLM / llama.cpp.
# AGENT: STATE / SIDE EFFECTS: localhost HTTP only unless FXSTACK_AGENT_ALLOW_REMOTE_LLM=true; never opens a server port.
# AGENT: SEE: docs/agents/model-stack-and-feature-flow.md ; fxstack/improve/proposer.py
from __future__ import annotations

from fxstack.llm.client import (
    LLMClient,
    LLMHealth,
    LLMUnavailable,
    NullLLMClient,
    OllamaClient,
    OpenAICompatClient,
    build_llm_client,
    is_local_url,
)

__all__ = [
    "LLMClient",
    "LLMHealth",
    "LLMUnavailable",
    "NullLLMClient",
    "OllamaClient",
    "OpenAICompatClient",
    "build_llm_client",
    "is_local_url",
]

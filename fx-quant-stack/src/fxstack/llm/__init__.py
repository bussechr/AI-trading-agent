# AGENT: ROLE: Local-first LLM client used by the self-improvement proposer ("LLM proposes").
# AGENT: ENTRYPOINT: `build_llm_client(settings)` -> LLMClient.
# AGENT: PRIMARY INPUTS: FXSTACK_LLM_* transport settings; callers supply deterministic request seeds.
# AGENT: PRIMARY OUTPUTS: schema-validated pydantic objects from local Ollama / vLLM / llama.cpp.
# AGENT: STATE / SIDE EFFECTS: localhost HTTP only unless FXSTACK_AGENT_ALLOW_REMOTE_LLM=true; never opens a server port.
# AGENT: SEE: docs/agents/model-stack-and-feature-flow.md ; fxstack/improve/proposer.py
from __future__ import annotations

from fxstack._lazy import bind_lazy_exports


_EXPORTS = {
    "LLMClient": "fxstack.llm.client",
    "LLMHealth": "fxstack.llm.client",
    "LLMUnavailable": "fxstack.llm.client",
    "NullLLMClient": "fxstack.llm.client",
    "OllamaClient": "fxstack.llm.client",
    "OpenAICompatClient": "fxstack.llm.client",
    "build_llm_client": "fxstack.llm.client",
    "is_local_url": "fxstack.llm.client",
    "WeightArtifact": "fxstack.llm.weights",
    "WeightManifest": "fxstack.llm.weights",
    "WeightError": "fxstack.llm.weights",
    "sha256_file": "fxstack.llm.weights",
    "verify_artifact": "fxstack.llm.weights",
    "verify_manifest": "fxstack.llm.weights",
    "load_manifest": "fxstack.llm.weights",
    "save_manifest": "fxstack.llm.weights",
    "download_artifact": "fxstack.llm.weights",
}

__getattr__, __dir__ = bind_lazy_exports(__name__, globals(), _EXPORTS)

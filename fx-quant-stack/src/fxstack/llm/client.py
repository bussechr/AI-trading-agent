"""Local-first LLM client for the strategy proposer.

Design principle (from the system goal): *the LLM proposes; deterministic code
disposes*. This module is only the "proposes" transport. It returns
schema-validated Pydantic objects and never decides anything itself.

Security posture:
- Default backend is ``null`` -- a fully offline mode that reports unavailable so
  the loop falls back to the deterministic heuristic proposer. No GPU, no network.
- Real backends (``ollama``, ``openai_compat`` for vLLM / llama.cpp) only talk to a
  loopback URL unless ``FXSTACK_AGENT_ALLOW_REMOTE_LLM=true``. We never bind a port;
  we only *call* a local server the operator started.
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from typing import Any, TypeVar
from urllib.parse import urlsplit

from pydantic import BaseModel, ValidationError

ModelT = TypeVar("ModelT", bound=BaseModel)


class LLMUnavailable(RuntimeError):
    """Raised when no usable local model is reachable.

    Callers (the proposer) treat this as a signal to fall back to the
    deterministic heuristic proposer rather than failing the loop.
    """


@dataclass(slots=True)
class LLMHealth:
    backend: str
    model: str
    base_url: str
    available: bool
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "model": self.model,
            "base_url": self.base_url,
            "available": bool(self.available),
            "reason": self.reason,
        }


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}


def is_local_url(url: str) -> bool:
    """True when ``url`` points at the loopback interface."""

    host = (urlsplit(str(url or "")).hostname or "").strip().lower()
    if not host:
        return False
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _schema_instruction(schema: type[BaseModel]) -> str:
    spec = json.dumps(schema.model_json_schema(), sort_keys=True)
    return (
        "Respond with a SINGLE JSON object and nothing else. No markdown, no prose, "
        "no code fences. The object MUST validate against this JSON Schema:\n"
        f"{spec}"
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    """Best-effort recovery of the first JSON object in ``text``."""

    raw = str(text or "").strip()
    if not raw:
        raise ValueError("empty model response")
    # Strip common code-fence wrappers.
    if raw.startswith("```"):
        raw = raw.strip("`")
        nl = raw.find("\n")
        if nl != -1:
            raw = raw[nl + 1 :]
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        parsed = json.loads(raw[start : end + 1])
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("no JSON object found in model response")


class LLMClient:
    """Abstract local LLM transport."""

    backend = "base"

    def __init__(self, *, model: str, base_url: str = "", max_retries: int = 2) -> None:
        self.model = str(model)
        self.base_url = str(base_url)
        self.max_retries = max(0, int(max_retries))

    def health(self) -> LLMHealth:  # pragma: no cover - overridden
        raise NotImplementedError

    def _complete_json(self, *, system: str, prompt: str, seed: int, temperature: float) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def generate_structured(
        self,
        *,
        schema: type[ModelT],
        prompt: str,
        system: str = "",
        seed: int = 0,
        temperature: float = 0.4,
    ) -> ModelT:
        """Return a validated instance of ``schema`` or raise ``LLMUnavailable``."""

        full_system = (str(system).strip() + "\n\n" + _schema_instruction(schema)).strip()
        last_error = ""
        attempt_prompt = str(prompt)
        for attempt in range(self.max_retries + 1):
            try:
                raw = self._complete_json(
                    system=full_system,
                    prompt=attempt_prompt,
                    seed=int(seed) + attempt,
                    temperature=float(temperature),
                )
                payload = _extract_json_object(raw)
                return schema.model_validate(payload)
            except LLMUnavailable:
                raise
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                last_error = str(exc)
                attempt_prompt = (
                    f"{prompt}\n\nYour previous reply was invalid: {last_error}\n"
                    "Return ONLY a corrected JSON object."
                )
            except Exception as exc:  # transport-level failure
                raise LLMUnavailable(f"{self.backend} transport error: {exc}") from exc
        raise LLMUnavailable(f"{self.backend} produced no schema-valid output: {last_error}")


class NullLLMClient(LLMClient):
    """Offline mode: reports unavailable so the heuristic proposer takes over."""

    backend = "null"

    def health(self) -> LLMHealth:
        return LLMHealth(
            backend=self.backend,
            model=self.model,
            base_url="",
            available=False,
            reason="offline heuristic mode (no local model configured)",
        )

    def _complete_json(self, *, system: str, prompt: str, seed: int, temperature: float) -> str:
        raise LLMUnavailable("null backend: deterministic heuristic proposer in use")


@dataclass(slots=True)
class _HttpOptions:
    timeout_s: float = 60.0
    api_key: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)


class _HttpClient(LLMClient):
    """Shared HTTP plumbing for local model servers."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        max_retries: int = 2,
        timeout_s: float = 60.0,
        api_key: str = "",
        allow_remote: bool = False,
    ) -> None:
        super().__init__(model=model, base_url=base_url, max_retries=max_retries)
        if not allow_remote and not is_local_url(base_url):
            raise LLMUnavailable(
                f"refusing non-local LLM URL {base_url!r}; set FXSTACK_AGENT_ALLOW_REMOTE_LLM=true to override"
            )
        self._opts = _HttpOptions(timeout_s=float(timeout_s), api_key=str(api_key))

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        import requests  # local import keeps the offline path dependency-free

        headers = {"Content-Type": "application/json", **self._opts.extra_headers}
        if self._opts.api_key:
            headers["Authorization"] = f"Bearer {self._opts.api_key}"
        url = f"{self.base_url.rstrip('/')}{path}"
        resp = requests.post(url, json=payload, headers=headers, timeout=self._opts.timeout_s)
        resp.raise_for_status()
        out = resp.json()
        return dict(out) if isinstance(out, dict) else {}

    def _ping(self, path: str) -> bool:
        import requests

        try:
            resp = requests.get(f"{self.base_url.rstrip('/')}{path}", timeout=min(5.0, self._opts.timeout_s))
            return bool(resp.status_code < 500)
        except Exception:
            return False


class OllamaClient(_HttpClient):
    """Talks to a local Ollama server (``/api/chat``)."""

    backend = "ollama"

    def health(self) -> LLMHealth:
        ok = self._ping("/api/tags")
        return LLMHealth(
            backend=self.backend,
            model=self.model,
            base_url=self.base_url,
            available=ok,
            reason="" if ok else "ollama not reachable on local port",
        )

    def _complete_json(self, *, system: str, prompt: str, seed: int, temperature: float) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "options": {"temperature": float(temperature), "seed": int(seed)},
        }
        data = self._post("/api/chat", payload)
        message = dict(data.get("message") or {})
        return str(message.get("content") or data.get("response") or "")


class OpenAICompatClient(_HttpClient):
    """Talks to an OpenAI-compatible local server (vLLM / llama.cpp ``/v1/chat/completions``)."""

    backend = "openai_compat"

    def health(self) -> LLMHealth:
        ok = self._ping("/v1/models")
        return LLMHealth(
            backend=self.backend,
            model=self.model,
            base_url=self.base_url,
            available=ok,
            reason="" if ok else "openai-compatible server not reachable on local port",
        )

    def _complete_json(self, *, system: str, prompt: str, seed: int, temperature: float) -> str:
        payload = {
            "model": self.model,
            "temperature": float(temperature),
            "seed": int(seed),
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        data = self._post("/v1/chat/completions", payload)
        choices = list(data.get("choices") or [])
        if not choices:
            return ""
        message = dict(dict(choices[0]).get("message") or {})
        return str(message.get("content") or "")


def build_llm_client(settings: Any | None = None) -> LLMClient:
    """Construct the configured local LLM client.

    Falls back to :class:`NullLLMClient` whenever the backend is ``null``/unknown or
    a real backend cannot be constructed safely (e.g. non-local URL without override).
    """

    if settings is None:
        from fxstack.settings import get_settings

        settings = get_settings()

    backend = str(getattr(settings, "llm_backend", "null") or "null").strip().lower()
    model = str(getattr(settings, "llm_model", "") or "local-model")
    base_url = str(getattr(settings, "llm_base_url", "") or "")
    timeout_s = float(getattr(settings, "llm_timeout_s", 60.0) or 60.0)
    max_retries = int(getattr(settings, "llm_max_retries", 2) or 0)
    api_key = str(getattr(settings, "llm_api_key", "") or "")
    allow_remote = bool(getattr(settings, "agent_allow_remote_llm", False))

    if backend in {"", "null", "off", "none", "heuristic", "offline"}:
        return NullLLMClient(model=model)
    try:
        if backend == "ollama":
            return OllamaClient(
                model=model,
                base_url=base_url,
                timeout_s=timeout_s,
                max_retries=max_retries,
                api_key=api_key,
                allow_remote=allow_remote,
            )
        if backend in {"openai_compat", "openai", "vllm", "llamacpp", "llama_cpp"}:
            return OpenAICompatClient(
                model=model,
                base_url=base_url,
                timeout_s=timeout_s,
                max_retries=max_retries,
                api_key=api_key,
                allow_remote=allow_remote,
            )
    except LLMUnavailable:
        return NullLLMClient(model=model)
    return NullLLMClient(model=model)

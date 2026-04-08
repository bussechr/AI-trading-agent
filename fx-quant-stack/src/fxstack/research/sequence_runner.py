from __future__ import annotations

import importlib.util
from dataclasses import asdict, dataclass
from typing import Any

from fxstack.settings import get_settings


@dataclass(slots=True)
class ResearchAdapterSpec:
    name: str
    runtime_role: str
    optional_dependency: str
    enabled: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def itransformer_scaffold() -> dict[str, Any]:
    return ResearchAdapterSpec(
        name="itransformer",
        runtime_role="research_only",
        optional_dependency="time_series_library",
        enabled=False,
    ).to_dict()


def research_runner_diagnostics() -> dict[str, Any]:
    s = get_settings()
    modules = {
        "torch": importlib.util.find_spec("torch") is not None,
        "transformers": importlib.util.find_spec("transformers") is not None,
    }
    ok = bool(modules["torch"] and modules["transformers"])
    return {
        "ok": ok,
        "runtime_role": "gpu_sequence_research_runner",
        "require_cuda": bool(s.require_cuda),
        "sequence_dataset_cache_root": str(s.sequence_dataset_cache_root),
        "modules": modules,
        "adapters": [itransformer_scaffold()],
    }


def main() -> None:
    print(research_runner_diagnostics())


if __name__ == "__main__":
    main()

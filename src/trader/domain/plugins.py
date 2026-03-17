from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class StrategyPlugin(Protocol):
    name: str

    def enabled(self, cfg: dict[str, Any]) -> bool:
        ...


@dataclass(slots=True)
class FlagPlugin:
    name: str
    flag: str

    def enabled(self, cfg: dict[str, Any]) -> bool:
        return bool(cfg.get(self.flag, False))


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, StrategyPlugin] = {}

    def register(self, plugin: StrategyPlugin) -> None:
        self._plugins[str(plugin.name)] = plugin

    def names(self) -> list[str]:
        return sorted(self._plugins.keys())

    def enabled(self, cfg: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for name, plugin in self._plugins.items():
            try:
                if plugin.enabled(cfg):
                    out.append(name)
            except Exception:
                continue
        return sorted(out)


def default_plugin_registry() -> PluginRegistry:
    reg = PluginRegistry()
    reg.register(FlagPlugin(name="hawkes", flag="use_hawkes"))
    reg.register(FlagPlugin(name="lppls", flag="use_lppls"))
    reg.register(FlagPlugin(name="heston", flag="use_heston_guard"))
    reg.register(FlagPlugin(name="ai_indicator", flag="use_ai_indicator_model"))
    return reg

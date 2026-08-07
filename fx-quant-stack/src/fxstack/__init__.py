"""Public package surface without eager runtime initialization."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .settings import Settings, get_settings

__all__ = ["Settings", "get_settings"]


def __getattr__(name: str) -> object:
    if name in __all__:
        from .settings import Settings, get_settings

        exports = {"Settings": Settings, "get_settings": get_settings}
        globals().update(exports)
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

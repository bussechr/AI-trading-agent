"""Small helper for side-effect-free package export surfaces."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Any

LazyExportTarget = str | tuple[str, str]


class _DeferredModule(ModuleType):
    """Module-shaped proxy that hydrates itself on first attribute access."""

    def __init__(self, target: str) -> None:
        super().__init__(target)
        self._target = target
        self._module: ModuleType | None = None

    def __getattr__(self, name: str) -> Any:
        module = self._module
        if module is None:
            module = import_module(self._target)
            self._module = module
        return getattr(module, name)


class _DeferredAttribute:
    """Attribute-shaped proxy that resolves its owner module on first use."""

    __slots__ = ("_attribute", "_module", "_target")

    def __init__(self, target: str, attribute: str) -> None:
        self._target = target
        self._attribute = attribute
        self._module: ModuleType | None = None

    def _resolve(self) -> Any:
        module = self._module
        if module is None:
            module = import_module(self._target)
            self._module = module
        return getattr(module, self._attribute)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._resolve()(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)


lazy_pandas: Any = _DeferredModule("pandas")
lazy_numpy: Any = _DeferredModule("numpy")


def deferred_module(target: str) -> Any:
    """Return a module-shaped proxy that imports ``target`` on first use."""

    return _DeferredModule(target)


def deferred_attribute(module_name: str, attribute: str) -> Any:
    """Return an attribute proxy supporting both calls and class attributes."""

    return _DeferredAttribute(module_name, attribute)


def deferred_callable(module_name: str, attribute: str) -> Any:
    """Return a callable that resolves its implementation on first use."""

    module: ModuleType | None = None

    def invoke(*args: Any, **kwargs: Any) -> Any:
        nonlocal module
        if module is None:
            module = import_module(module_name)
        return getattr(module, attribute)(*args, **kwargs)

    invoke.__name__ = attribute
    return invoke


def lazy_get_settings() -> Any:
    """Resolve the cached settings singleton without importing it eagerly."""

    from fxstack.settings import get_settings

    return get_settings()


def bind_lazy_exports(
    package: str,
    namespace: dict[str, Any],
    exports: dict[str, LazyExportTarget],
):
    """Return PEP 562 hooks that import and cache named exports on demand."""

    namespace["__all__"] = list(exports)

    def resolve(name: str) -> Any:
        target = exports.get(name)
        if target is None:
            raise AttributeError(f"module {package!r} has no attribute {name!r}")
        module_name, attribute = target if isinstance(target, tuple) else (target, name)
        value = getattr(import_module(module_name), attribute)
        namespace[name] = value
        return value

    def exported_names() -> list[str]:
        return sorted(set(namespace) | set(exports))

    return resolve, exported_names

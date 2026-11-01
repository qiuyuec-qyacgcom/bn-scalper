"""策略注册表：名字 -> 工厂函数。

扩展方式：
    from .registry import register
    @register("my_strategy")
    class MyStrategy(Strategy): ...
然后在 TOML 里写 `[strategy] name = "my_strategy"` 即可，无需改动引擎代码。
"""

from __future__ import annotations

from typing import Any, Callable

from .base import Strategy

_FACTORIES: dict[str, Callable[[dict[str, Any]], Strategy]] = {}


def register(name: str) -> Callable[[type[Strategy]], type[Strategy]]:
    def deco(cls: type[Strategy]) -> type[Strategy]:
        cls.name = name
        _FACTORIES[name] = lambda params: cls(params)  # type: ignore[call-arg]
        return cls

    return deco


def create_strategy(name: str, params: dict[str, Any] | None = None) -> Strategy:
    if name not in _FACTORIES:
        raise KeyError(f"未注册的策略 {name!r}，可用: {sorted(_FACTORIES)}")
    return _FACTORIES[name](params or {})


def available() -> list[str]:
    return sorted(_FACTORIES)

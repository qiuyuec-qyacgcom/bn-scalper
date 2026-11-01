"""最小 TOML 写入器。

标准库只有 `tomllib`（只读），后台要"改配置"就必须能写。这里不追求覆盖
TOML 1.0 全部语法，只支持本项目配置用到的子集：
标量 / 布尔 / 字符串 / 数字 / 字符串数组 / 嵌套表。

刻意保留 key 的书写顺序（dict 有序），这样 diff 只反映真实改动。
"""

from __future__ import annotations

import json
import math


class TomlWriteError(ValueError):
    pass


def _key(key: str) -> str:
    if not key:
        raise TomlWriteError("空 key")
    if all(ch.isalnum() or ch in "-_" for ch in key):
        return key
    return json.dumps(key, ensure_ascii=False)


def _scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise TomlWriteError("不支持 NaN/Inf")
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_scalar(v) for v in value) + "]"
    raise TomlWriteError(f"不支持的类型: {type(value).__name__}")


def _emit_table(lines: list[str], table: dict, prefix: tuple[str, ...]) -> None:
    scalars: list[tuple[str, object]] = []
    subtables: list[tuple[str, dict]] = []
    for key, value in table.items():
        if isinstance(value, dict):
            subtables.append((key, value))
        else:
            scalars.append((key, value))

    if prefix:
        lines.append("")
        lines.append("[" + ".".join(_key(p) for p in prefix) + "]")

    for key, value in scalars:
        lines.append(f"{_key(key)} = {_scalar(value)}")

    for key, value in subtables:
        _emit_table(lines, value, prefix + (key,))


def dumps(data: dict) -> str:
    if not isinstance(data, dict):
        raise TomlWriteError("顶层必须是 dict")
    lines: list[str] = []
    _emit_table(lines, data, ())
    return "\n".join(lines).strip() + "\n"

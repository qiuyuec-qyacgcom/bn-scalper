"""AI 辅助层：智能填充 / 自动纠错 / 自然语言增改 / 结果解读。

设计原则
--------
1. **无密钥也必须可用**：没有配置大模型时自动降级为「本地规则模式」，
   四项能力都用确定性算法实现（类型纠正、范围夹紧、命令关键词解析、错误码模板），
   界面会明确标注当前模式，绝不假装调用了模型。
2. **AI 只产出建议，不直接落库**：自然语言命令返回的是**计划（plan）**，
   必须由用户确认后走 `/api/admin/ai/apply`，且执行前仍要通过 schema 校验。
3. **模型输出必须可解析**：强制 JSON；解析失败或字段越界就回退到本地结果，
   并把原因记在 `notes` 里，让用户知道"这次没问到模型"。
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .schema import ENTITIES, EntitySpec, Issue

log = logging.getLogger(__name__)

ACTIONS = ("create", "update", "delete", "list")


class AIUnavailable(RuntimeError):
    """模型不可用（未配置 / 网络失败 / 响应不可解析）。"""


# --------------------------------------------------------------------------- #
# 配置与客户端
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class AIConfig:
    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    timeout: float = 20.0

    @classmethod
    def from_env(cls) -> "AIConfig":
        forced_off = str(os.environ.get("SCALPX_AI", "")).strip().lower() in ("0", "off", "false")
        key = os.environ.get("SCALPX_AI_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        enabled = bool(key) and not forced_off
        return cls(
            enabled=enabled,
            base_url=(os.environ.get("SCALPX_AI_BASE_URL") or "https://api.openai.com/v1").rstrip("/"),
            api_key=key,
            model=os.environ.get("SCALPX_AI_MODEL") or "gpt-4o-mini",
            timeout=float(os.environ.get("SCALPX_AI_TIMEOUT") or 20.0),
        )

    def describe(self) -> dict:
        return {
            "mode": "llm" if self.enabled else "local",
            "model": self.model if self.enabled else "",
            "base_url": self.base_url if self.enabled else "",
        }


class AIClient:
    """极简 OpenAI 兼容客户端（只用标准库）。"""

    def __init__(self, cfg: AIConfig) -> None:
        self.cfg = cfg

    @property
    def available(self) -> bool:
        return self.cfg.enabled and bool(self.cfg.api_key)

    def chat_json(self, system: str, user: str, *, max_tokens: int = 900) -> dict:
        if not self.available:
            raise AIUnavailable("未配置模型")
        payload = json.dumps(
            {
                "model": self.cfg.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            f"{self.cfg.base_url}/chat/completions",
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.cfg.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "scalpx-web",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AIUnavailable(f"模型请求失败：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise AIUnavailable("模型响应不是合法 JSON") from exc

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise AIUnavailable("模型响应结构异常") from exc

        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise AIUnavailable("模型返回内容无法解析为 JSON") from exc


# --------------------------------------------------------------------------- #
# Schema 摘要（控制提示词体积）
# --------------------------------------------------------------------------- #
def _schema_digest(spec: EntitySpec) -> dict:
    fields = []
    for f in spec.fields:
        item: dict[str, Any] = {"name": f.name, "type": f.type, "required": f.required}
        if f.min is not None or f.max is not None:
            item["range"] = [f.min, f.max]
        if f.type == "enum":
            opts = f.options() if callable(f.options) else (f.options or [])
            item["enum"] = [o["value"] for o in opts]
        if f.max_length:
            item["max_length"] = f.max_length
        fields.append(item)
    return {"entity": spec.key, "label": spec.label, "description": spec.description, "fields": fields}


def _all_digests() -> list[dict]:
    return [_schema_digest(s) for s in ENTITIES.values()]


# --------------------------------------------------------------------------- #
# 本地规则实现（无模型时的兜底，必须真的有用）
# --------------------------------------------------------------------------- #
def _alias_lookup(spec: EntitySpec, key: str) -> str | None:
    """把用户写的键映射到字段名：支持英文字段名、完整中文标签、标签后缀。

    例：`名称` → `数据源名称`，`symbol` → `symbol`。
    """
    key = (key or "").strip().lower()
    if not key:
        return None
    for f in spec.fields:
        if key == f.name.lower() or key == f.label.lower():
            return f.name
    for f in spec.fields:
        if key in f.label.lower() or f.label.lower().endswith(key):
            return f.name
    return None


def _local_suggest(spec: EntitySpec, partial: dict, hint: str) -> dict:
    values: dict[str, Any] = {}
    notes: list[str] = []

    # 1) 从提示词里抽取 key=value（中文标签、标签后缀都认）
    for key, val in re.findall(r"([\w\u4e00-\u9fa5]+)\s*[=:：]\s*([^\s,，;；]+)", hint or ""):
        target = _alias_lookup(spec, key)
        if target and target not in partial:
            values[target] = val
            notes.append(f"从描述中识别到 {target} = {val}")

    # 2) 空字段填默认值 / 枚举首项
    for f in spec.fields:
        if f.name in partial or f.name in values or f.readonly:
            continue
        if f.default not in (None, ""):
            values[f.name] = f.default
        elif f.type == "enum":
            opts = f.options() if callable(f.options) else (f.options or [])
            if opts:
                values[f.name] = opts[0]["value"]
                notes.append(f"{f.label} 取默认项 {opts[0]['value']}")

    if not notes:
        notes.append("没有可自动推断的字段，请手动填写必填项")
    return {"mode": "local", "values": values, "notes": notes, "confidence": 0.4}


def _local_fix(spec: EntitySpec, payload: dict, issues: list[dict]) -> dict:
    """按字段类型与范围做确定性修复。"""
    fixed = dict(payload)
    changes: list[dict] = []
    unresolved: list[dict] = []

    for issue in issues:
        name = issue.get("field", "")
        spec_field = spec.field_spec(name)
        if spec_field is None:
            # 未知字段：直接丢弃，避免"以为改了其实没生效"
            for key in name.split(","):
                key = key.strip()
                if key and key in fixed:
                    fixed.pop(key)
                    changes.append({"field": key, "from": "(存在)", "to": "(已移除)", "why": "未知字段"})
            continue

        raw = fixed.get(name)
        if spec_field.required and raw in (None, ""):
            if spec_field.default is not None:
                fixed[name] = spec_field.default
                changes.append({"field": name, "from": "", "to": spec_field.default, "why": "填充默认值"})
            elif spec_field.type == "enum":
                opts = spec_field.options() if callable(spec_field.options) else (spec_field.options or [])
                if opts:
                    fixed[name] = opts[0]["value"]
                    changes.append({"field": name, "from": "", "to": opts[0]["value"], "why": "取枚举首项"})
            else:
                unresolved.append({"field": name, "message": "必填项缺失且无默认值"})
            continue

        if raw in (None, ""):
            continue

        value, err = spec_field.coerce(raw)
        if err is None and value != raw:
            fixed[name] = value
            changes.append({"field": name, "from": raw, "to": value, "why": "类型纠正"})
            raw = value

        if spec_field.type in ("int", "float"):
            num = fixed.get(name)
            if spec_field.min is not None and isinstance(num, (int, float)) and num < spec_field.min:
                fixed[name] = spec_field.min
                changes.append({"field": name, "from": num, "to": spec_field.min, "why": "夹紧到下限"})
            elif spec_field.max is not None and isinstance(num, (int, float)) and num > spec_field.max:
                fixed[name] = spec_field.max
                changes.append({"field": name, "from": num, "to": spec_field.max, "why": "夹紧到上限"})

        if spec_field.type in ("string", "text", "enum") and isinstance(fixed.get(name), str):
            trimmed = fixed[name].strip()
            if trimmed != fixed[name]:
                fixed[name] = trimmed
                changes.append({"field": name, "from": repr(raw), "to": trimmed, "why": "去除首尾空白"})

        unresolved.append({"field": name, "message": issue.get("message", "未自动修复")})

    resolved_fields = {c["field"] for c in changes}
    unresolved = [u for u in unresolved if u["field"] not in resolved_fields]
    return {"mode": "local", "payload": fixed, "changes": changes, "unresolved": unresolved}


_ACTION_WORDS = {
    "create": ("新增", "添加", "创建", "新建", "add", "create", "insert"),
    "update": ("修改", "更新", "编辑", "改一下", "update", "edit", "modify"),
    "delete": ("删除", "移除", "删掉", "delete", "remove"),
    "list": ("查看", "列出", "查询", "list", "show"),
}


def _local_command(text: str) -> dict:
    lowered = text.lower()
    action = "create"
    for name, words in _ACTION_WORDS.items():
        if any(w in lowered for w in words):
            action = name
            break

    entity = ""
    for spec in ENTITIES.values():
        # 中文标签、英文 key、以及 key 的自然写法（data sources / datasources）都接受
        candidates = (spec.label, spec.key, spec.key.replace("_", " "), spec.key.replace("_", ""))
        if any(c and (c in text or c.lower() in lowered) for c in candidates):
            entity = spec.key
            break

    warnings: list[str] = []
    if not entity:
        warnings.append("没能识别出要操作的数据类型，请写明「交易账户 / 策略实例 / 风控档案 / 数据源」")

    spec = ENTITIES.get(entity)
    data: dict[str, Any] = {}
    if spec is not None:
        for key, val in re.findall(r"([\w\u4e00-\u9fa5]+)\s*[=:：]\s*([^\s,，;；]+)", text):
            target = _alias_lookup(spec, key)
            if target:
                data[target] = val

    rid = None
    match = re.search(r"(?:id|编号)\s*[=:：]?\s*(\d+)", lowered)
    if match:
        rid = int(match.group(1))

    if action in ("update", "delete") and rid is None:
        warnings.append("修改/删除需要指定 id，例如「修改 id=3 的风控档案 name=保守」")

    summary = f"{action} {spec.label if spec else '未知类型'}"
    if data:
        summary += "：" + "，".join(f"{k}={v}" for k, v in data.items())
    return {
        "mode": "local",
        "plan": {
            "entity": entity,
            "action": action,
            "id": rid,
            "data": data,
            "summary": summary,
            "warnings": warnings,
        },
        "confidence": 0.5 if entity else 0.2,
    }


def _local_explain(action: str, ok: bool, message: str, issues: list[dict], data: Any) -> dict:
    if ok:
        label = {"create": "创建成功", "update": "更新成功", "delete": "删除成功", "list": "查询完成"}.get(action, "操作成功")
        suggestions = []
        if isinstance(data, dict) and data.get("enabled") is False:
            suggestions.append("该记录当前为停用状态，需要在引擎中生效请把「启用」打开")
        if isinstance(data, dict) and action == "create":
            suggestions.append("新记录已保存，可继续编辑或直接用于回测/实盘配置")
        return {
            "mode": "local",
            "summary": f"{label}。{message}".strip(),
            "suggestions": suggestions,
            "severity": "ok",
        }

    hints: list[str] = []
    if "DUPLICATE" in message or "重复" in message:
        hints.append("名称类字段要求全局唯一，换一个名字，或直接编辑已有记录")
    for issue in issues:
        field = issue.get("field", "")
        msg = issue.get("message", "")
        if "必填" in msg:
            hints.append(f"{field} 是必填项，请补全")
        elif "格式" in msg:
            hints.append(f"{field} 格式不符合要求，请参考字段说明")
        elif "不能大于" in msg or "不能小于" in msg:
            hints.append(f"{field} 超出允许范围，请调整到区间内")
        elif "JSON" in msg:
            hints.append(f"{field} 需要合法的 JSON 对象，可用 AI 纠错自动修复")
    if not hints:
        hints.append("可点击「AI 纠错」尝试自动修复，或按字段提示手动调整")
    return {"mode": "local", "summary": message or "操作失败", "suggestions": hints, "severity": "error"}


# --------------------------------------------------------------------------- #
# 对外门面
# --------------------------------------------------------------------------- #
class AIAssistant:
    def __init__(self, cfg: AIConfig | None = None) -> None:
        self.cfg = cfg or AIConfig.from_env()
        self.client = AIClient(self.cfg)

    def status(self) -> dict:
        return self.cfg.describe()

    # ------------------------------------------------------------------ #
    def suggest(self, spec: EntitySpec, partial: dict, hint: str) -> dict:
        local = _local_suggest(spec, partial, hint)
        if not self.client.available:
            return local
        try:
            result = self.client.chat_json(
                "你是量化交易系统的表单助手。只返回 JSON："
                '{"values": {字段名: 值}, "notes": ["说明"]}。'
                "不要编造无法从描述推断的值；不确定就不要填。",
                json.dumps({"schema": _schema_digest(spec), "current": partial, "hint": hint}, ensure_ascii=False),
            )
            values = result.get("values") or {}
            if not isinstance(values, dict):
                raise AIUnavailable("values 不是对象")
            unknown = [k for k in values if spec.field_spec(k) is None]
            for key in unknown:
                values.pop(key, None)
            merged = {**local["values"], **values}
            return {
                "mode": "llm",
                "values": merged,
                "notes": (result.get("notes") or []) + ([f"已忽略未知字段 {unknown}"] if unknown else []),
                "confidence": 0.8,
            }
        except AIUnavailable as exc:
            log.info("AI 填充回退本地规则：%s", exc)
            return {**local, "notes": local["notes"] + [f"模型不可用（{exc}），已使用本地规则"]}

    # ------------------------------------------------------------------ #
    def fix(self, spec: EntitySpec, payload: dict, issues: list[dict]) -> dict:
        local = _local_fix(spec, payload, issues)
        if not self.client.available:
            return local
        try:
            result = self.client.chat_json(
                "你是数据校验修复助手。根据字段报错修正 payload，只返回 JSON："
                '{"payload": {...}, "changes": [{"field","from","to","why"}], "unresolved": [{"field","message"}]}。'
                "不得新增未知字段，不得突破取值范围。",
                json.dumps(
                    {
                        "schema": _schema_digest(spec),
                        "payload": payload,
                        "issues": issues,
                    },
                    ensure_ascii=False,
                ),
            )
            fixed = result.get("payload")
            if not isinstance(fixed, dict):
                raise AIUnavailable("payload 不是对象")
            fixed = {k: v for k, v in fixed.items() if spec.field_spec(k) is not None}
            return {
                "mode": "llm",
                "payload": fixed,
                "changes": result.get("changes") or [],
                "unresolved": result.get("unresolved") or [],
            }
        except AIUnavailable as exc:
            log.info("AI 纠错回退本地规则：%s", exc)
            return {**local, "changes": local["changes"] + [{"field": "-", "from": "-", "to": "-", "why": f"模型不可用：{exc}"}]}

    # ------------------------------------------------------------------ #
    def command(self, text: str) -> dict:
        local = _local_command(text)
        if not self.client.available:
            return {**local, "requires_confirmation": True}
        try:
            result = self.client.chat_json(
                "你是后台操作的意图解析器。把用户的话转成结构化计划，只返回 JSON："
                '{"entity":"<实体key>","action":"create|update|delete|list","id":<数字或null>,'
                '"data":{字段:值},"summary":"一句话说明你会做什么","warnings":["风险提示"]}。'
                "entity 必须来自给定列表；无法确定时 entity 留空并在 warnings 说明。",
                json.dumps({"entities": _all_digests(), "text": text}, ensure_ascii=False),
            )
            entity = str(result.get("entity") or "")
            action = str(result.get("action") or "")
            spec = ENTITIES.get(entity)
            warnings = list(result.get("warnings") or [])
            if spec is None:
                warnings.append("模型未能匹配到合法实体，已回退到本地解析")
                raise AIUnavailable("entity 非法")
            if action not in ACTIONS:
                warnings.append(f"非法动作 {action}，已回退")
                raise AIUnavailable("action 非法")
            data = result.get("data") or {}
            if not isinstance(data, dict):
                data = {}
            data = {k: v for k, v in data.items() if spec.field_spec(k) is not None}
            rid = result.get("id")
            rid = int(rid) if isinstance(rid, (int, str)) and str(rid).isdigit() else None
            return {
                "mode": "llm",
                "requires_confirmation": True,
                "confidence": 0.85,
                "plan": {
                    "entity": entity,
                    "action": action,
                    "id": rid,
                    "data": data,
                    "summary": result.get("summary") or f"{action} {spec.label}",
                    "warnings": warnings,
                },
            }
        except AIUnavailable as exc:
            log.info("AI 指令回退本地解析：%s", exc)
            return {
                **local,
                "requires_confirmation": True,
                "plan": {**local["plan"], "warnings": local["plan"]["warnings"] + [f"模型不可用（{exc}）"]},
            }

    # ------------------------------------------------------------------ #
    def explain(self, spec: EntitySpec | None, action: str, ok: bool, message: str, issues: list[dict], data: Any) -> dict:
        local = _local_explain(action, ok, message, issues, data)
        if not self.client.available:
            return local
        try:
            result = self.client.chat_json(
                "你是运维助手。用一两句话解读这次操作的结果并给出下一步建议，只返回 JSON："
                '{"summary":"...","suggestions":["..."],"severity":"ok|warn|error"}。简洁、可执行、不要复述原文。',
                json.dumps(
                    {
                        "entity": spec.label if spec else "",
                        "action": action,
                        "ok": ok,
                        "message": message,
                        "issues": issues,
                        "data": data if isinstance(data, (dict, list)) else str(data),
                    },
                    ensure_ascii=False,
                ),
            )
            return {
                "mode": "llm",
                "summary": result.get("summary") or local["summary"],
                "suggestions": result.get("suggestions") or local["suggestions"],
                "severity": result.get("severity") or local["severity"],
            }
        except AIUnavailable as exc:
            log.info("AI 解读回退本地模板：%s", exc)
            return local


# --------------------------------------------------------------------------- #
# 计划校验与执行（AI 只给计划，执行仍走 schema）
# --------------------------------------------------------------------------- #
def validate_plan(plan: dict) -> tuple[dict, list[Issue]]:
    issues: list[Issue] = []
    entity = str(plan.get("entity") or "")
    action = str(plan.get("action") or "")
    spec = ENTITIES.get(entity)
    if spec is None:
        issues.append(Issue("entity", f"未知实体：{entity or '(空)'}"))
    if action not in ACTIONS:
        issues.append(Issue("action", f"未知动作：{action or '(空)'}"))

    data = plan.get("data") or {}
    if not isinstance(data, dict):
        issues.append(Issue("data", "data 必须是对象"))
        data = {}
    if spec is not None and not issues:
        unknown = [k for k in data if spec.field_spec(k) is None]
        if unknown:
            issues.append(Issue("data", f"包含未知字段：{', '.join(unknown)}"))

    rid = plan.get("id")
    if action in ("update", "delete"):
        if not isinstance(rid, int) or rid <= 0:
            issues.append(Issue("id", "该操作需要指定正整数 id"))

    return {"entity": entity, "action": action, "id": rid, "data": data}, issues

"""实体注册表：用声明式 schema 描述"后台能管什么数据"。

为什么不用一张万能 key-value 表
------------------------------
万能表（entity/attr/value）看似能装下一切，代价是：无法建唯一索引、无法做类型约束、
无法用 SQL 过滤排序、迁移时无从校验。所以这里采用**每个实体一张真实表 + 一份 schema**：
存储是强类型的，而增删改查、校验、表单渲染全部由 schema 驱动，新增实体只加一段声明。

字段类型与校验
--------------
- `string` / `text` / `json` / `int` / `float` / `bool` / `enum`
- 通用规则：required / min / max / pattern / max_length / options / default / readonly
- `options` 可以是静态列表，也可以是**可调用对象**（运行时求值），
  这样策略名、市场列表等动态枚举不需要硬编码。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

FIELD_TYPES = ("string", "text", "json", "int", "float", "bool", "enum")


@dataclass(slots=True)
class Issue:
    field: str
    message: str

    def as_dict(self) -> dict:
        return {"field": self.field, "message": self.message}


@dataclass(slots=True)
class FieldSpec:
    name: str
    label: str
    type: str = "string"
    required: bool = False
    default: Any = None
    min: float | None = None
    max: float | None = None
    max_length: int | None = None
    pattern: str | None = None
    options: list[dict] | Callable[[], list[dict]] | None = None
    help: str = ""
    widget: str = ""          # 前端提示：csv / textarea / password / number
    readonly: bool = False
    placeholder: str = ""

    # 序列化给前端
    def as_dict(self) -> dict:
        opts = self.options() if callable(self.options) else (self.options or [])
        return {
            "name": self.name,
            "label": self.label,
            "type": self.type,
            "required": self.required,
            "default": self.default,
            "min": self.min,
            "max": self.max,
            "max_length": self.max_length,
            "pattern": self.pattern,
            "options": opts,
            "help": self.help,
            "widget": self.widget or ("textarea" if self.type == "text" else ""),
            "readonly": self.readonly,
            "placeholder": self.placeholder,
        }

    # ------------------------------------------------------------------ #
    def coerce(self, raw: Any) -> tuple[Any, str | None]:
        """把表单/JSON 传来的值转成目标类型。返回 (值, 错误)。"""
        if self.type == "bool":
            if isinstance(raw, bool):
                return raw, None
            if isinstance(raw, (int, float)):
                return bool(raw), None
            text = str(raw).strip().lower()
            if text in ("1", "true", "yes", "on", "是"):
                return True, None
            if text in ("0", "false", "no", "off", "否", ""):
                return False, None
            return False, "应为布尔值"

        if self.type == "int":
            if isinstance(raw, bool):
                return int(raw), None
            try:
                return int(float(str(raw).strip())), None
            except (TypeError, ValueError):
                return 0, "应为整数"

        if self.type == "float":
            try:
                return float(str(raw).strip()), None
            except (TypeError, ValueError):
                return 0.0, "应为数字"

        if self.type == "json":
            if isinstance(raw, (dict, list)):
                return raw, None
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                return {}, None
            try:
                return json.loads(str(raw)), None
            except (TypeError, ValueError):
                return {}, "不是合法 JSON"

        # string / text / enum
        return ("" if raw is None else str(raw)), None

    def validate(self, raw: Any, *, partial: bool = False) -> tuple[Any, list[Issue]]:
        issues: list[Issue] = []

        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if self.required and not partial:
                return None, [Issue(self.name, f"{self.label}为必填项")]
            return None, issues

        value, err = self.coerce(raw)
        if err:
            return None, [Issue(self.name, f"{self.label}{err}")]

        if self.type in ("string", "text", "enum"):
            value = str(value).strip()
            if self.max_length and len(value) > self.max_length:
                issues.append(Issue(self.name, f"{self.label}不能超过 {self.max_length} 个字符"))
            if self.pattern and not re.match(self.pattern, value):
                issues.append(Issue(self.name, f"{self.label}格式不符"))
            if self.type == "enum":
                allowed = [str(o["value"]) for o in (self.options() if callable(self.options) else (self.options or []))]
                if allowed and value not in allowed:
                    issues.append(Issue(self.name, f"{self.label}取值必须是 {'/'.join(allowed)} 之一"))

        if self.type in ("int", "float"):
            if self.min is not None and value < self.min:
                issues.append(Issue(self.name, f"{self.label}不能小于 {self.min}"))
            if self.max is not None and value > self.max:
                issues.append(Issue(self.name, f"{self.label}不能大于 {self.max}"))

        return value, issues


@dataclass(slots=True)
class EntitySpec:
    key: str
    label: str
    table: str
    description: str
    fields: list[FieldSpec]
    unique: list[tuple[str, ...]] = field(default_factory=list)
    search_fields: list[str] = field(default_factory=list)
    order_by: str = "id ASC"
    reorderable: bool = False
    icon: str = "◆"
    # 领域专属的不变量（如"代码必须符合该市场规则"）用 hook 扩展：
    #   hook(payload, partial) -> (规范化后的 payload, issues)
    hook: Callable[[dict, bool], tuple[dict, list[Issue]]] | None = None

    def field_spec(self, name: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.name == name), None)

    @property
    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "icon": self.icon,
            "reorderable": self.reorderable,
            "unique": [list(u) for u in self.unique],
            "search_fields": self.search_fields,
            "fields": [f.as_dict() for f in self.fields],
        }

    # ------------------------------------------------------------------ #
    def normalize(self, payload: dict, *, partial: bool = False) -> tuple[dict, list[Issue]]:
        """逐字段校验并规范化。`partial=True` 时只处理出现的字段（PATCH）。"""
        issues: list[Issue] = []
        out: dict[str, Any] = {}

        for spec in self.fields:
            if spec.readonly:
                continue
            if partial and spec.name not in payload:
                continue
            raw = payload.get(spec.name, spec.default)
            value, field_issues = spec.validate(raw, partial=partial)
            issues.extend(field_issues)
            if value is not None and not field_issues:
                out[spec.name] = value

        # 未知字段直接拒绝，避免"以为改了其实没生效"
        known = set(self.field_names)
        unknown = [k for k in payload if k not in known and not k.startswith("_")]
        if unknown:
            issues.append(Issue(",".join(unknown), f"未知字段：{', '.join(unknown)}"))

        if self.hook is not None and not issues:
            out, hook_issues = self.hook(out, partial)
            issues.extend(hook_issues)

        return out, issues


# --------------------------------------------------------------------------- #
# 内置实体
# --------------------------------------------------------------------------- #
def _opt(value: str, label: str) -> dict:
    return {"value": value, "label": label}


_VENUES = [
    _opt("BINANCE", "币安"), _opt("OKX", "OKX"), _opt("BYBIT", "Bybit"),
    _opt("COINBASE", "Coinbase"), _opt("CME", "CME"), _opt("ICE", "ICE"),
    _opt("IBKR", "盈透证券"),
]

_INTERVALS = [_opt(v, v) for v in ("1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d")]

_NAME_FIELD = dict(type="string", required=True, min=0, max_length=32,
                   pattern=r"^[\w\u4e00-\u9fa5\-]{2,32}$")

BROKER_ACCOUNTS = EntitySpec(
    key="broker_accounts",
    label="交易账户",
    table="broker_accounts",
    description="交易所/券商连接与费率配置。密钥只存环境变量名，不落库。",
    icon="⚡",
    unique=[("name",)],
    search_fields=["name", "venue"],
    fields=[
        FieldSpec(name="name", label="账户名称", **_NAME_FIELD, placeholder="如 币安-主账户"),
        FieldSpec(name="venue", label="交易所", type="enum", required=True, options=_VENUES, default="BINANCE"),
        FieldSpec(
            name="account_type", label="账户类型", type="enum", required=True,
            options=[_opt("futures", "合约"), _opt("spot", "现货"), _opt("margin", "杠杆")],
            default="futures",
        ),
        FieldSpec(name="api_key_env", label="密钥环境变量名", required=True,
                  default="BINANCE_API_KEY", pattern=r"^[A-Z][A-Z0-9_]{2,40}$",
                  help="只记录变量名，密钥本身由部署环境注入"),
        FieldSpec(name="testnet", label="使用测试网", type="bool", default=True),
        FieldSpec(name="leverage", label="杠杆倍数", type="float", min=1, max=20, default=3),
        FieldSpec(
            name="position_mode", label="持仓模式", type="enum",
            options=[_opt("oneway", "单向"), _opt("hedge", "双向")], default="oneway",
            help="双向模式需要交易所侧同时开启，否则会拒单",
        ),
        FieldSpec(name="maker_fee_bps", label="maker 费率(bps)", type="float", min=0, max=100, default=2),
        FieldSpec(name="taker_fee_bps", label="taker 费率(bps)", type="float", min=0, max=100, default=4),
        FieldSpec(name="enabled", label="启用", type="bool", default=True),
        FieldSpec(name="note", label="备注", type="text", max_length=200),
    ],
)

STRATEGY_INSTANCES = EntitySpec(
    key="strategy_instances",
    label="策略实例",
    table="strategy_instances",
    description="策略 + 参数 + 标的的组合（一个策略可挂多个实例，各自独立参数）。",
    icon="◈",
    unique=[("name",)],
    search_fields=["name", "strategy"],
    fields=[
        FieldSpec(name="name", label="实例名称", **_NAME_FIELD, placeholder="如 BTC趋势-保守"),
        FieldSpec(name="strategy", label="策略", type="enum", required=True,
                  options=[], help="来自策略注册表"),
        FieldSpec(name="symbols", label="标的", required=True, widget="csv",
                  placeholder="BTCUSDT,ETHUSDT", help="逗号分隔；需先在标的库登记"),
        FieldSpec(name="timeframe", label="周期", type="enum", required=True, options=_INTERVALS, default="1m"),
        FieldSpec(name="params", label="参数(JSON)", type="json", default={},
                  widget="textarea", help='覆盖策略默认参数，如 {"rr": 1.5}'),
        FieldSpec(name="risk_profile", label="风控档案", default="", max_length=32,
                  help="引用风控档案名称，留空用全局配置"),
        FieldSpec(name="enabled", label="启用", type="bool", default=True),
        FieldSpec(name="note", label="备注", type="text", max_length=200),
    ],
)

RISK_PROFILES = EntitySpec(
    key="risk_profiles",
    label="风控档案",
    table="risk_profiles",
    description="可复用的风险参数组，对应引擎里的 [risk] 段。",
    icon="⛨",
    unique=[("name",)],
    search_fields=["name"],
    fields=[
        FieldSpec(name="name", label="档案名称", **_NAME_FIELD, placeholder="如 保守-0.1%"),
        FieldSpec(name="risk_per_trade", label="单笔风险", type="float", required=True,
                  min=0.0001, max=0.02, default=0.0025, help="占权益比例，0.0025 = 0.25%"),
        FieldSpec(name="max_positions", label="最大持仓数", type="int", min=1, max=20, default=2),
        FieldSpec(name="max_leverage", label="最大杠杆", type="float", min=1, max=20, default=3),
        FieldSpec(name="max_daily_loss_pct", label="日亏熔断", type="float", min=0.001, max=0.5, default=0.025),
        FieldSpec(name="max_daily_trades", label="日交易上限", type="int", min=1, max=1000, default=40),
        FieldSpec(name="consecutive_loss_halt", label="连亏停手笔数", type="int", min=1, max=50, default=5),
        FieldSpec(name="cooldown_minutes", label="连亏冷却(分)", type="int", min=0, max=1440, default=30),
        FieldSpec(name="min_stop_pct", label="最小止损%", type="float", min=0.0005, max=0.2, default=0.004),
        FieldSpec(name="max_stop_pct", label="最大止损%", type="float", min=0.001, max=0.5, default=0.008),
        FieldSpec(name="max_cost_ratio", label="成本/风险上限", type="float", min=0, max=1, default=0.35,
                  help="往返成本占单笔风险的比例上限"),
        FieldSpec(name="enabled", label="启用", type="bool", default=True),
        FieldSpec(name="note", label="备注", type="text", max_length=200),
    ],
)

DATA_SOURCES = EntitySpec(
    key="data_sources",
    label="数据源",
    table="data_sources",
    description="K 线 / 逐笔 / 盘口 / 基本面数据的采集与保留策略。",
    icon="⛁",
    unique=[("name",)],
    search_fields=["name", "symbol", "kind"],
    fields=[
        FieldSpec(name="name", label="数据源名称", **_NAME_FIELD, placeholder="如 BTC-1m-K线"),
        FieldSpec(
            name="kind", label="数据类型", type="enum", required=True,
            options=[
                _opt("kline", "K 线"), _opt("aggtrade", "逐笔成交"),
                _opt("orderbook", "盘口快照"), _opt("fundamental", "基本面"),
            ],
            default="kline",
        ),
        FieldSpec(name="venue", label="来源", type="enum", required=True, options=_VENUES, default="BINANCE"),
        FieldSpec(name="symbol", label="标的代码", required=True, max_length=24,
                  pattern=r"^[A-Z0-9.\-]{2,24}$", placeholder="BTCUSDT"),
        FieldSpec(name="interval", label="周期", type="enum", required=True, options=_INTERVALS, default="1m"),
        FieldSpec(name="path", label="存储路径", default="", max_length=200,
                  widget="", help="本地 CSV/SQLite 路径，留空用默认 data/raw"),
        FieldSpec(name="retention_days", label="保留天数", type="int", min=0, max=3650, default=90,
                  help="0 表示永久保留"),
        FieldSpec(name="enabled", label="启用", type="bool", default=True),
        FieldSpec(name="note", label="备注", type="text", max_length=200),
    ],
)

# 注册表：新增实体只需在这里追加一个 EntitySpec
ENTITIES: dict[str, EntitySpec] = {
    spec.key: spec
    for spec in (BROKER_ACCOUNTS, STRATEGY_INSTANCES, RISK_PROFILES, DATA_SOURCES)
}


def get_entity(key: str) -> EntitySpec | None:
    return ENTITIES.get(key)


def entity_catalog() -> list[dict]:
    return [spec.as_dict() for spec in ENTITIES.values()]


def iter_issues(issues: Iterable[Issue]) -> list[dict]:
    return [i.as_dict() for i in issues]

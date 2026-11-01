"""标的库：资产类型注册表、代码规范化与合法性校验。

设计要点
--------
1. **类型/市场是数据，不是 if-else**：新增一个市场只需在 `ASSET_TYPES` 里加一行，
   校验、前端下拉、提示文案自动生效。
2. **规范化先行**：`BTC/USDT`、`btc-usdt`、`btcusdt` 统一成 `BTCUSDT` 后再判重，
   否则同一个标的能注册三遍。
3. **判重键是 `type:market:code`**：`000001` 在深市是平安银行、在沪市是上证指数，
   不能跨市场误杀；但同一市场下必须唯一。
4. 校验函数是**纯函数**，不碰数据库，方便单测覆盖所有边界。

注意：本模块只负责"标的元数据"，不保证该标的在本地有行情数据。
能否回测由调用方（api 层）另行校验，两者不要混为一谈。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# 资产类型注册表
# --------------------------------------------------------------------------- #
# 每个市场: (显示名, 代码正则, 示例/提示)
ASSET_TYPES: dict[str, dict[str, Any]] = {
    "crypto": {
        "label": "加密货币",
        "default_market": "BINANCE",
        "default_currency": "USDT",
        "markets": {
            "BINANCE": ("币安", r"^[A-Z0-9]{5,24}$", "BASE+QUOTE，如 BTCUSDT"),
            "OKX": ("OKX", r"^[A-Z0-9]{5,24}$", "如 BTCUSDT"),
            "BYBIT": ("Bybit", r"^[A-Z0-9]{5,24}$", "如 ETHUSDT"),
            "COINBASE": ("Coinbase", r"^[A-Z0-9]{5,24}$", "如 BTCUSD"),
        },
    },
    "stock": {
        "label": "股票",
        "default_market": "SH",
        "default_currency": "CNY",
        "markets": {
            "SH": ("沪市", r"^\d{6}$", "6 位数字，如 600519"),
            "SZ": ("深市", r"^\d{6}$", "6 位数字，如 000001"),
            "BJ": ("北交所", r"^\d{6}$", "6 位数字，如 830799"),
            "HK": ("港股", r"^\d{5}$", "5 位数字，如 00700"),
            "US": ("美股", r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$", "如 AAPL / BRK.A"),
            "JP": ("日股", r"^\d{4}$", "4 位数字，如 7203"),
        },
    },
    "fund": {
        "label": "基金",
        "default_market": "CN",
        "default_currency": "CNY",
        "markets": {
            "CN": ("境内", r"^\d{6}$", "6 位数字，如 510300"),
            "HK": ("香港", r"^\d{5}$", "5 位数字"),
            "US": ("美国", r"^[A-Z]{1,5}$", "如 SPY"),
        },
    },
    "bond": {
        "label": "债券",
        "default_market": "CN",
        "default_currency": "CNY",
        "markets": {
            "CN": ("境内", r"^\d{6}$", "6 位数字，如 019547"),
            "US": ("美国", r"^[A-Z0-9]{5,12}$", "如 US10Y"),
        },
    },
    "futures": {
        "label": "期货",
        "default_market": "SHFE",
        "default_currency": "CNY",
        "markets": {
            "SHFE": ("上期所", r"^[A-Z]{1,2}\d{3,4}$", "品种+月份，如 RB2410"),
            "DCE": ("大商所", r"^[A-Z]{1,2}\d{3,4}$", "如 I2409"),
            "CZCE": ("郑商所", r"^[A-Z]{1,2}\d{3,4}$", "如 MA409"),
            "CFFEX": ("中金所", r"^[A-Z]{1,2}\d{4}$", "如 IF2409"),
            "INE": ("能源中心", r"^[A-Z]{1,2}\d{3,4}$", "如 SC2410"),
            "CME": ("CME", r"^[A-Z]{1,3}[FGHJKMNQUVXZ]\d{1,2}$", "如 ESZ4"),
            "ICE": ("ICE", r"^[A-Z]{1,3}[FGHJKMNQUVXZ]\d{1,2}$", "如 BRNZ4"),
        },
    },
    "forex": {
        "label": "外汇",
        "default_market": "FX",
        "default_currency": "USD",
        "markets": {
            "FX": ("即期", r"^[A-Z]{6}$", "6 位货币对，如 EURUSD"),
        },
    },
}

ASSET_TYPE_KEYS = tuple(ASSET_TYPES.keys())

# 允许在代码里出现的分隔符（规范化时移除）
_SEPARATORS = re.compile(r"[\s/\-_]")


@dataclass(slots=True)
class Issue:
    field: str
    message: str

    def as_dict(self) -> dict:
        return {"field": self.field, "message": self.message}


@dataclass(slots=True)
class Validated:
    ok: bool
    data: dict = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "valid": self.ok,
            "data": self.data,
            "issues": [i.as_dict() for i in self.issues],
        }


# --------------------------------------------------------------------------- #
# 规范化
# --------------------------------------------------------------------------- #
def normalize_code(raw: str, asset_type: str) -> str:
    """统一成大写、去分隔符。仅做形态归一，不做合法性判断。"""
    code = _SEPARATORS.sub("", str(raw or "").strip()).upper()
    if asset_type in ("stock", "fund", "bond", "futures"):
        # 阿拉伯数字代码保留前导零（00700 不能变成 700）
        pass
    return code


def canonical_key(asset_type: str, market: str, code: str) -> str:
    return f"{asset_type}:{market}:{code.upper()}"


def describe(asset_type: str, market: str) -> str:
    t = ASSET_TYPES.get(asset_type)
    if not t:
        return ""
    m = t["markets"].get(market)
    return f"{t['label']}·{m[0]}" if m else t["label"]


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def _to_float(value: Any, default: float) -> tuple[float, str | None]:
    if value is None or value == "":
        return default, None
    try:
        return float(value), None
    except (TypeError, ValueError):
        return default, "必须是数字"


def validate_instrument(payload: dict, *, partial: bool = False) -> Validated:
    """校验并规范化一条标的。

    `partial=True` 用于 PATCH：只校验出现的字段。
    """
    issues: list[Issue] = []
    out: dict[str, Any] = {}

    asset_type = str(payload.get("asset_type") or "").strip().lower()
    if not asset_type:
        if not partial:
            issues.append(Issue("asset_type", "请选择资产类型"))
    elif asset_type not in ASSET_TYPES:
        issues.append(Issue("asset_type", f"不支持的资产类型：{asset_type}"))
    else:
        out["asset_type"] = asset_type
        spec = ASSET_TYPES[asset_type]

        market = str(payload.get("market") or spec["default_market"]).strip().upper()
        if market not in spec["markets"]:
            issues.append(
                Issue("market", f"{spec['label']}不支持市场 {market}，可选：{'/'.join(spec['markets'])}")
            )
        else:
            out["market"] = market

        # --- 代码 ---
        raw_code = payload.get("code")
        if not raw_code:
            if not partial:
                issues.append(Issue("code", "代码不能为空"))
        else:
            code = normalize_code(raw_code, asset_type)
            if len(code) > 24:
                issues.append(Issue("code", "代码长度不能超过 24"))
            elif "market" in out:
                pattern, hint = spec["markets"][out["market"]][1], spec["markets"][out["market"]][2]
                if not re.match(pattern, code):
                    issues.append(Issue("code", f"代码格式不符：{hint}"))
                else:
                    out["code"] = code

        # --- 名称 ---
        name = str(payload.get("name") or "").strip()
        if not name:
            if not partial:
                issues.append(Issue("name", "名称不能为空"))
        elif len(name) > 64:
            issues.append(Issue("name", "名称不能超过 64 个字符"))
        else:
            out["name"] = name

        # --- 计价币 ---
        currency = str(payload.get("currency") or spec["default_currency"]).strip().upper()
        if not re.match(r"^[A-Z]{2,10}$", currency):
            issues.append(Issue("currency", "计价币应为 2-10 位字母，如 CNY/USD/USDT"))
        else:
            out["currency"] = currency

    # --- 数值属性（与资产类型无关） ---
    for field_name, default, lo, hi, desc in (
        ("price_tick", 0.01, 1e-8, 1e6, "最小价格变动"),
        ("qty_step", 1.0, 1e-8, 1e9, "最小数量变动"),
        ("min_notional", 0.0, 0.0, 1e12, "最小名义金额"),
        ("lot_size", 0.0, 0.0, 1e12, "每手数量（0=不适用）"),
    ):
        if partial and field_name not in payload:
            continue
        value, err = _to_float(payload.get(field_name), default)
        if err:
            issues.append(Issue(field_name, f"{desc} {err}"))
        elif not (lo <= value <= hi):
            issues.append(Issue(field_name, f"{desc}需在 [{lo}, {hi}] 之间"))
        else:
            out[field_name] = value

    if "is_active" in payload or not partial:
        raw_active = payload.get("is_active", True)
        out["is_active"] = bool(raw_active) if not isinstance(raw_active, str) else raw_active.lower() in ("1", "true", "yes")

    if "meta" in payload or "meta_json" in payload:
        meta = payload.get("meta", payload.get("meta_json"))
        if meta is None:
            meta = {}
        if isinstance(meta, str):
            try:
                import json

                meta = json.loads(meta) if meta.strip() else {}
            except ValueError:
                issues.append(Issue("meta", "扩展属性必须是合法 JSON"))
                meta = {}
        if not isinstance(meta, dict):
            issues.append(Issue("meta", "扩展属性必须是对象"))
            meta = {}
        else:
            import json

            if len(json.dumps(meta, ensure_ascii=False)) > 2048:
                issues.append(Issue("meta", "扩展属性过大（上限 2KB）"))
        out["meta"] = meta

    if not issues:
        if "asset_type" in out and "market" in out and "code" in out:
            out["symbol_key"] = canonical_key(out["asset_type"], out["market"], out["code"])
    return Validated(ok=not issues, data=out, issues=issues)


# --------------------------------------------------------------------------- #
# 批量导入文本解析
# --------------------------------------------------------------------------- #
def parse_batch_text(text: str) -> list[dict]:
    """解析批量导入文本。

    支持三种写法（自动识别分隔符）：
        BTCUSDT,比特币, crypto, BINANCE
        BTCUSDT\t比特币\tcrypto\tBINANCE
        BTCUSDT 比特币 crypto BINANCE        # 以 2+ 空格分隔
    列顺序固定为 code,name,asset_type[,market[,currency[,price_tick[,qty_step]]]]
    以 # 开头的行与空行会被忽略。
    """
    import csv
    import io

    rows: list[dict] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" in line:
            parts = line.split("\t")
        elif "," in line:
            parts = next(csv.reader(io.StringIO(line)))
        else:
            # 无显式分隔符时按空白切分；名字里若含空格请改用逗号/制表符
            parts = line.split()
        parts = [p.strip() for p in parts]
        item: dict[str, Any] = {
            "code": parts[0] if len(parts) > 0 else "",
            "name": parts[1] if len(parts) > 1 else "",
            "asset_type": parts[2] if len(parts) > 2 else "",
        }
        if len(parts) > 3 and parts[3]:
            item["market"] = parts[3]
        if len(parts) > 4 and parts[4]:
            item["currency"] = parts[4]
        if len(parts) > 5 and parts[5]:
            item["price_tick"] = parts[5]
        if len(parts) > 6 and parts[6]:
            item["qty_step"] = parts[6]
        rows.append(item)
    return rows


def type_options() -> list[dict]:
    """给前端用的类型/市场元数据（含正则与提示）。"""
    out = []
    for key, spec in ASSET_TYPES.items():
        out.append(
            {
                "value": key,
                "label": spec["label"],
                "default_market": spec["default_market"],
                "default_currency": spec["default_currency"],
                "markets": [
                    {"value": m, "label": v[0], "pattern": v[1], "hint": v[2]}
                    for m, v in spec["markets"].items()
                ],
            }
        )
    return out

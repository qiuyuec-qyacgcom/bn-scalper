"""通用数据管理：schema 校验、实体存储、AI 本地规则。

重点验证三件事：
1. schema 是**唯一真相**——类型、范围、必填、枚举、未知字段都在这一层拦住；
2. 存储层不做业务判断，但必须保证唯一性与错误可枚举；
3. 没有大模型时，四项 AI 能力仍然**真的有用**（类型纠正、范围夹紧、指令解析、错误解读）。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from scalpx.web.ai import (
    AIClient,
    AIConfig,
    AIAssistant,
    AIUnavailable,
    TTLCache,
    _local_command,
    _local_fix,
    _local_suggest,
    validate_plan,
)
from scalpx.web.generic import EntityError, EntityStore
from scalpx.web.repo import WebRepo
from scalpx.web.schema import ENTITIES, get_entity


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="scalpx-entity-")
        self.web = WebRepo(Path(self.tmp) / "web.db")
        self.store = EntityStore(self.web)

    def tearDown(self) -> None:
        self.web.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestSchemaValidation(unittest.TestCase):
    def test_entity_registry_complete(self) -> None:
        self.assertEqual(
            set(ENTITIES), {"broker_accounts", "strategy_instances", "risk_profiles", "data_sources"}
        )
        for spec in ENTITIES.values():
            self.assertTrue(spec.fields, spec.key)
            self.assertTrue(spec.unique, f"{spec.key} 应声明唯一键")
            for f in spec.fields:
                self.assertIn(f.type, ("string", "text", "json", "int", "float", "bool", "enum"))

    def test_required_and_unknown_fields(self) -> None:
        spec = get_entity("broker_accounts")
        assert spec is not None
        _, issues = spec.normalize({"venue": "BINANCE"})
        fields = {i.field for i in issues}
        self.assertIn("name", fields)
        # api_key_env 有默认值，缺省时由 schema 补齐而不是报错
        data, _ = spec.normalize({"name": "acc", "venue": "BINANCE"})
        self.assertEqual(data["api_key_env"], "BINANCE_API_KEY")

        _, issues2 = spec.normalize({"name": "acc1", "api_key_env": "BINANCE_API_KEY", "venue": "BINANCE", "bogus": 1})
        self.assertTrue(any("未知字段" in i.message for i in issues2))

    def test_enum_and_range(self) -> None:
        spec = get_entity("risk_profiles")
        assert spec is not None
        _, issues = spec.normalize({"name": "p1", "risk_per_trade": 0.5})
        self.assertTrue(any(i.field == "risk_per_trade" for i in issues))

        spec2 = get_entity("data_sources")
        assert spec2 is not None
        _, issues2 = spec2.normalize({"name": "d1", "kind": "tick", "symbol": "BTCUSDT"})
        self.assertTrue(any(i.field == "kind" for i in issues2))

    def test_type_coercion(self) -> None:
        spec = get_entity("broker_accounts")
        assert spec is not None
        data, issues = spec.normalize(
            {"name": "acc", "api_key_env": "BINANCE_API_KEY", "venue": "BINANCE",
             "leverage": "5", "testnet": "false", "maker_fee_bps": "1.5"}
        )
        self.assertFalse(issues, [i.message for i in issues])
        self.assertEqual(data["leverage"], 5.0)
        self.assertIs(data["testnet"], False)
        self.assertAlmostEqual(data["maker_fee_bps"], 1.5)

    def test_json_field(self) -> None:
        spec = get_entity("strategy_instances")
        assert spec is not None
        data, issues = spec.normalize(
            {"name": "s1", "strategy": "trend_pullback", "symbols": "BTCUSDT",
             "params": '{"rr": 1.5}'}
        )
        self.assertFalse(issues, [i.message for i in issues])
        self.assertEqual(data["params"], {"rr": 1.5})

        _, bad = spec.normalize(
            {"name": "s1", "strategy": "trend_pullback", "symbols": "BTCUSDT", "params": "{oops"}
        )
        self.assertTrue(any(i.field == "params" for i in bad))


class TestEntityStore(StoreCase):
    def _account(self, **over):
        payload = {"name": "币安-主账户", "venue": "BINANCE", "api_key_env": "BINANCE_API_KEY"}
        payload.update(over)
        return payload

    def test_create_applies_defaults(self) -> None:
        spec = get_entity("broker_accounts")
        assert spec is not None
        item = self.store.create(spec, self._account())
        self.assertEqual(item["name"], "币安-主账户")
        self.assertEqual(item["account_type"], "futures")
        self.assertIs(item["testnet"], True)
        self.assertIs(item["enabled"], True)
        self.assertGreater(item["id"], 0)

    def test_create_validation_failure(self) -> None:
        spec = get_entity("broker_accounts")
        assert spec is not None
        with self.assertRaises(EntityError) as ctx:
            self.store.create(spec, {"venue": "BINANCE"})
        self.assertEqual(ctx.exception.code, "VALIDATION_FAILED")
        self.assertTrue(ctx.exception.issues)

    def test_duplicate_unique_field(self) -> None:
        spec = get_entity("broker_accounts")
        assert spec is not None
        self.store.create(spec, self._account())
        with self.assertRaises(EntityError) as ctx:
            self.store.create(spec, self._account(note="另一个"))
        self.assertEqual(ctx.exception.code, "DUPLICATE")
        self.assertEqual(ctx.exception.status, 409)

    def test_update_merges_and_revalidates(self) -> None:
        spec = get_entity("risk_profiles")
        assert spec is not None
        item = self.store.create(spec, {"name": "保守", "risk_per_trade": 0.001})
        updated = self.store.update(spec, item["id"], {"risk_per_trade": 0.002})
        self.assertAlmostEqual(updated["risk_per_trade"], 0.002)
        self.assertEqual(updated["max_positions"], 2)  # 未提交的字段保持原值

        with self.assertRaises(EntityError):
            self.store.update(spec, item["id"], {"risk_per_trade": 99})

    def test_update_duplicate_against_other_row(self) -> None:
        spec = get_entity("data_sources")
        assert spec is not None
        a = self.store.create(spec, {"name": "ds-a", "kind": "kline", "symbol": "BTCUSDT"})
        self.store.create(spec, {"name": "ds-b", "kind": "kline", "symbol": "BTCUSDT"})
        with self.assertRaises(EntityError) as ctx:
            self.store.update(spec, a["id"], {"name": "ds-b"})
        self.assertEqual(ctx.exception.code, "DUPLICATE")

    def test_delete_and_missing(self) -> None:
        spec = get_entity("data_sources")
        assert spec is not None
        item = self.store.create(spec, {"name": "tmp", "kind": "aggtrade", "symbol": "ETHUSDT"})
        self.store.delete(spec, item["id"])
        with self.assertRaises(EntityError) as ctx:
            self.store.get(spec, item["id"])
        self.assertEqual(ctx.exception.code, "NOT_FOUND")

    def test_list_and_search(self) -> None:
        spec = get_entity("data_sources")
        assert spec is not None
        self.store.create(spec, {"name": "btc-kline", "kind": "kline", "symbol": "BTCUSDT"})
        self.store.create(spec, {"name": "eth-trade", "kind": "aggtrade", "symbol": "ETHUSDT"})
        self.assertEqual(len(self.store.list(spec)), 2)
        self.assertEqual(len(self.store.list(spec, q="ETH")), 1)
        self.assertEqual(self.store.list(spec, q="BTC")[0]["name"], "btc-kline")

    def test_json_roundtrip(self) -> None:
        spec = get_entity("strategy_instances")
        assert spec is not None
        item = self.store.create(
            spec,
            {"name": "s1", "strategy": "trend_pullback", "symbols": "BTCUSDT",
             "params": {"rr": 1.5, "max_hold_bars": 30}},
        )
        self.assertEqual(item["params"]["rr"], 1.5)
        self.assertEqual(item["params"]["max_hold_bars"], 30)

    def test_validate_endpoint_shape(self) -> None:
        spec = get_entity("broker_accounts")
        assert spec is not None
        good = self.store.validate(spec, self._account())
        self.assertTrue(good["valid"])
        self.assertEqual(good["issues"], [])

        bad = self.store.validate(spec, {"venue": "BINANCE"})
        self.assertFalse(bad["valid"])
        self.assertTrue(bad["issues"])

        self.store.create(spec, self._account())
        dup = self.store.validate(spec, self._account())
        self.assertFalse(dup["valid"])
        self.assertIsNotNone(dup["duplicate"])


class TestLocalAI(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = get_entity("data_sources")
        assert self.spec is not None

    def test_suggest_fills_defaults_and_parses_hint(self) -> None:
        result = _local_suggest(self.spec, {}, "新增一个 BTC 的 K 线数据源，symbol=BTCUSDT，名称=btc-1m")
        self.assertEqual(result["mode"], "local")
        self.assertEqual(result["values"].get("symbol"), "BTCUSDT")
        self.assertEqual(result["values"].get("name"), "btc-1m")
        self.assertEqual(result["values"].get("kind"), "kline")  # 枚举默认
        self.assertTrue(result["notes"])

    def test_fix_coerces_and_clamps(self) -> None:
        payload = {"name": "  x  ", "kind": "kline", "symbol": "BTCUSDT", "retention_days": "9999"}
        issues = [{"field": "retention_days", "message": "保留天数不能大于 3650"}]
        fixed = _local_fix(self.spec, payload, issues)
        self.assertEqual(fixed["payload"]["retention_days"], 3650)
        self.assertTrue(any(c["field"] == "retention_days" for c in fixed["changes"]))

    def test_fix_drops_unknown_field(self) -> None:
        payload = {"name": "x", "kind": "kline", "symbol": "BTCUSDT", "hack": 1}
        fixed = _local_fix(self.spec, payload, [{"field": "hack", "message": "未知字段"}])
        self.assertNotIn("hack", fixed["payload"])

    def test_command_parses_create(self) -> None:
        result = _local_command("新增数据源 name=demo kind=kline symbol=BTCUSDT interval=5m")
        plan = result["plan"]
        self.assertEqual(plan["entity"], "data_sources")
        self.assertEqual(plan["action"], "create")
        self.assertEqual(plan["data"]["symbol"], "BTCUSDT")
        self.assertEqual(plan["data"]["interval"], "5m")

    def test_command_update_requires_id(self) -> None:
        result = _local_command("修改风控档案 name=保守")
        self.assertEqual(result["plan"]["action"], "update")
        self.assertIsNone(result["plan"]["id"])
        self.assertTrue(any("id" in w for w in result["plan"]["warnings"]))

    def test_command_delete_with_id(self) -> None:
        plan = _local_command("删除 id=7 的数据源")["plan"]
        self.assertEqual(plan["action"], "delete")
        self.assertEqual(plan["id"], 7)

    def test_validate_plan_rejects_bad_input(self) -> None:
        _, issues = validate_plan({"entity": "nope", "action": "create", "data": {}})
        self.assertTrue(any(i.field == "entity" for i in issues))

        _, issues2 = validate_plan({"entity": "data_sources", "action": "explode"})
        self.assertTrue(any(i.field == "action" for i in issues2))

        _, issues3 = validate_plan({"entity": "data_sources", "action": "update", "id": None, "data": {}})
        self.assertTrue(any(i.field == "id" for i in issues3))

        _, issues4 = validate_plan({"entity": "data_sources", "action": "create", "data": {"zzz": 1}})
        self.assertTrue(any("未知字段" in i.message for i in issues4))

    def test_assistant_without_key_uses_local(self) -> None:
        assistant = AIAssistant(AIConfig(enabled=False))
        self.assertEqual(assistant.status()["mode"], "local")
        out = assistant.command("新增数据源 name=a kind=kline symbol=BTCUSDT")
        self.assertTrue(out["requires_confirmation"])
        self.assertEqual(out["mode"], "local")

        explain = assistant.explain(self.spec, "create", False, "名称重复", [], None)
        self.assertEqual(explain["severity"], "error")
        self.assertTrue(explain["suggestions"])

    def test_assistant_falls_back_when_model_unreachable(self) -> None:
        """配置了 key 但地址不可达 -> 必须回退本地，而不是报错给用户。"""
        assistant = AIAssistant(
            AIConfig(enabled=True, base_url="http://127.0.0.1:1", api_key="x", model="m", timeout=0.3)
        )
        out = assistant.suggest(self.spec, {}, "symbol=BTCUSDT")
        self.assertEqual(out["mode"], "local")
        self.assertEqual(out["values"].get("symbol"), "BTCUSDT")
        self.assertTrue(any("本地规则" in n for n in out["notes"]))


class TestAIConfigAndClient(unittest.TestCase):
    """模型连接的稳定性保障：配置收敛、重试、缓存、并发、降级策略。"""

    def test_overrides_clamp_and_ignore_bad_values(self) -> None:
        base = AIConfig(enabled=False, api_key="k")
        cfg = base.with_overrides(
            {"timeout": 9999, "max_retries": 99, "max_concurrency": 0, "fallback": "explode", "model": " m1 "}
        )
        self.assertEqual(cfg.timeout, 120.0)      # 上限收敛
        self.assertEqual(cfg.max_retries, 5)
        self.assertEqual(cfg.max_concurrency, 1)  # 下限收敛
        self.assertEqual(cfg.fallback, "local")   # 非法值被忽略，保留原值
        self.assertEqual(cfg.model, "m1")         # 字符串去空白

    def test_overrides_reject_bad_base_url(self) -> None:
        base = AIConfig(base_url="https://api.example.com/v1")
        cfg = base.with_overrides({"base_url": "file:///etc/passwd"})
        self.assertEqual(cfg.base_url, "https://api.example.com/v1")

    def test_describe_never_leaks_key(self) -> None:
        cfg = AIConfig(enabled=True, api_key="sk-secret-value")
        described = cfg.describe()
        self.assertTrue(described["key_configured"])
        self.assertNotIn("secret", json.dumps(described, ensure_ascii=False))

    def test_cache_hit_avoids_second_call(self) -> None:
        client = AIClient(AIConfig(enabled=True, api_key="k", cache_ttl=60, cache_size=8))
        calls = {"n": 0}

        def fake_post(payload):
            calls["n"] += 1
            return {"value": 1}

        client._post = fake_post  # type: ignore[method-assign]
        first = client.chat_json("sys", "user")
        second = client.chat_json("sys", "user")
        self.assertEqual(first, second)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(client.stats()["cache"]["hits"], 1)

    def test_retries_on_retryable_then_succeeds(self) -> None:
        client = AIClient(
            AIConfig(enabled=True, api_key="k", max_retries=2, retry_backoff=0.0, cache_ttl=0)
        )
        state = {"n": 0}

        def flaky(payload):
            state["n"] += 1
            if state["n"] < 3:
                raise AIUnavailable("网络抖动", retryable=True)
            return {"ok": 1}

        client._post = flaky  # type: ignore[method-assign]
        self.assertEqual(client.chat_json("s", "u"), {"ok": 1})
        self.assertEqual(state["n"], 3)
        self.assertEqual(client.stats()["retries"], 2)

    def test_no_retry_for_permanent_error(self) -> None:
        client = AIClient(
            AIConfig(enabled=True, api_key="k", max_retries=3, retry_backoff=0.0, cache_ttl=0)
        )
        state = {"n": 0}

        def denied(payload):
            state["n"] += 1
            raise AIUnavailable("鉴权失败", retryable=False)

        client._post = denied  # type: ignore[method-assign]
        with self.assertRaises(AIUnavailable):
            client.chat_json("s", "u")
        self.assertEqual(state["n"], 1)  # 只尝试一次

    def test_exhausted_retries_raise(self) -> None:
        client = AIClient(
            AIConfig(enabled=True, api_key="k", max_retries=1, retry_backoff=0.0, cache_ttl=0)
        )
        client._post = lambda p: (_ for _ in ()).throw(AIUnavailable("boom", retryable=True))  # type: ignore[method-assign]
        with self.assertRaises(AIUnavailable):
            client.chat_json("s", "u")
        self.assertEqual(client.stats()["retries"], 1)

    def test_cache_entry_expires(self) -> None:
        cache = TTLCache(ttl=0.05, size=4)
        cache.set("k", {"v": 1})
        self.assertEqual(cache.get("k"), {"v": 1})
        time.sleep(0.08)
        self.assertIsNone(cache.get("k"))

    def test_cache_respects_size(self) -> None:
        cache = TTLCache(ttl=60, size=2)
        cache.set("a", {})
        cache.set("b", {})
        cache.set("c", {})
        self.assertEqual(cache.stats()["entries"], 2)

    def test_fallback_error_mode_raises(self) -> None:
        spec = get_entity("data_sources")
        assert spec is not None
        assistant = AIAssistant(
            AIConfig(enabled=True, api_key="k", base_url="http://127.0.0.1:1",
                     timeout=0.2, max_retries=0, fallback="error")
        )
        with self.assertRaises(AIUnavailable):
            assistant.suggest(spec, {}, "symbol=BTCUSDT")

    def test_reload_rebuilds_client(self) -> None:
        assistant = AIAssistant(AIConfig(enabled=False))
        old = assistant.client
        status = assistant.reload({"timeout": 5, "max_retries": 1})
        self.assertIsNot(assistant.client, old)
        self.assertEqual(status["timeout"], 5)


if __name__ == "__main__":
    unittest.main()

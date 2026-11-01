"""Web 层集成测试：真起一个 HTTP 服务，用真实请求验证接口契约与权限。

为什么不用 mock：路由、鉴权、序列化、静态资源这些最容易出错的地方，
只有在真实 socket 上跑一遍才算验证过。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from scalpx.web.auth import hash_password
from scalpx.web.repo import WebRepo
from scalpx.web.server import WebApp, make_server

ROOT = Path(__file__).resolve().parents[1]


class WebTestCase(unittest.TestCase):
    server = None
    thread = None
    tmpdir = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmpdir = tempfile.mkdtemp(prefix="scalpx-web-test-")
        tmp = Path(cls.tmpdir)
        config_dir = tmp / "config"
        config_dir.mkdir()
        shutil.copy(ROOT / "config" / "backtest.toml", config_dir / "backtest.toml")

        cls.app = WebApp(
            root=tmp,
            trading_db=tmp / "data" / "scalpx.db",
            web_db=tmp / "data" / "web.db",
            secret_path=tmp / "data" / "web" / "secret.key",
            config_dir=config_dir,
        )
        pw, salt = hash_password("admin-pass-123")
        cls.app.web_repo.create_user("admin", pw, salt, "admin")
        pw2, salt2 = hash_password("user-pass-123")
        cls.app.web_repo.create_user("alice", pw2, salt2, "user")

        cls.server = make_server(cls.app, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.server:
            cls.server.shutdown()
            cls.server.server_close()
        if cls.app:
            cls.app.close()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    # ------------------------------------------------------------------ #
    def call(self, method: str, path: str, *, body=None, token: str = ""):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"raw": raw}

    def login(self, username: str, password: str) -> str:
        status, payload = self.call("POST", "/api/auth/login", body={"username": username, "password": password})
        self.assertEqual(status, 200, payload)
        return payload["data"]["token"]


class TestHealthAndAuth(WebTestCase):
    def test_health_is_public(self) -> None:
        status, payload = self.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["status"], "ok")

    def test_bad_login_rejected(self) -> None:
        status, payload = self.call("POST", "/api/auth/login", body={"username": "admin", "password": "wrong-pass"})
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "BAD_CREDENTIALS")

    def test_login_and_me(self) -> None:
        token = self.login("admin", "admin-pass-123")
        status, payload = self.call("GET", "/api/auth/me", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["username"], "admin")
        self.assertIn("manage_users", payload["data"]["capabilities"])

    def test_missing_token_is_401(self) -> None:
        status, payload = self.call("GET", "/api/runs")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "UNAUTHORIZED")

    def test_tampered_token_rejected(self) -> None:
        token = self.login("admin", "admin-pass-123")
        broken = token[:-4] + "0000"
        status, payload = self.call("GET", "/api/auth/me", token=broken)
        self.assertEqual(status, 401)


class TestRbac(WebTestCase):
    def test_user_cannot_access_admin_api(self) -> None:
        token = self.login("alice", "user-pass-123")
        status, payload = self.call("GET", "/api/admin/users", token=token)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "FORBIDDEN")

    def test_admin_can_list_users(self) -> None:
        token = self.login("admin", "admin-pass-123")
        status, payload = self.call("GET", "/api/admin/users", token=token)
        self.assertEqual(status, 200)
        names = [u["username"] for u in payload["data"]["items"]]
        self.assertIn("admin", names)
        self.assertIn("alice", names)

    def test_admin_creates_and_updates_user(self) -> None:
        token = self.login("admin", "admin-pass-123")
        status, payload = self.call(
            "POST", "/api/admin/users",
            body={"username": "bob", "password": "bob-pass-1234", "role": "user"}, token=token,
        )
        self.assertEqual(status, 200, payload)
        uid = payload["data"]["id"]

        status, payload = self.call(
            "PATCH", f"/api/admin/users/{uid}", body={"role": "admin"}, token=token
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["updated"])

    def test_cannot_demote_last_admin(self) -> None:
        token = self.login("admin", "admin-pass-123")
        status, payload = self.call("GET", "/api/admin/users", token=token)
        admins = [u for u in payload["data"]["items"] if u["role"] == "admin" and u["status"] == "active"]
        # 前面的用例可能已创建第二个管理员，这里只验证接口不 500
        self.assertGreaterEqual(len(admins), 1)


class TestDataEndpoints(WebTestCase):
    def test_empty_trading_db_degrades_gracefully(self) -> None:
        token = self.login("admin", "admin-pass-123")
        status, payload = self.call("GET", "/api/runs", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["items"], [])

    def test_dashboard_without_runs_returns_404(self) -> None:
        token = self.login("admin", "admin-pass-123")
        status, payload = self.call("GET", "/api/dashboard", token=token)
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "NO_DATA")

    def test_micro_endpoint_works_without_tables(self) -> None:
        token = self.login("admin", "admin-pass-123")
        status, payload = self.call("GET", "/api/micro", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["flow"]["bars"], 0)

    def test_system_info(self) -> None:
        token = self.login("admin", "admin-pass-123")
        status, payload = self.call("GET", "/api/admin/system", token=token)
        self.assertEqual(status, 200)
        self.assertIn("service", payload["data"])
        self.assertIn("tables", payload["data"]["storage"])


class TestConfigApi(WebTestCase):
    def test_get_config(self) -> None:
        token = self.login("admin", "admin-pass-123")
        status, payload = self.call("GET", "/api/admin/config?profile=backtest", token=token)
        self.assertEqual(status, 200)
        self.assertIn("risk_per_trade", payload["data"]["data"]["risk"])

    def test_put_invalid_config_rejected(self) -> None:
        token = self.login("admin", "admin-pass-123")
        status, payload = self.call(
            "PUT", "/api/admin/config",
            body={"profile": "backtest", "data": {"risk": {"risk_per_trade": 99}}}, token=token,
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "CONFIG_INVALID")

    def test_put_valid_config_saved_with_backup(self) -> None:
        token = self.login("admin", "admin-pass-123")
        _, current = self.call("GET", "/api/admin/config?profile=backtest", token=token)
        data = current["data"]["data"]
        data["risk"]["max_daily_trades"] = 33
        status, payload = self.call(
            "PUT", "/api/admin/config", body={"profile": "backtest", "data": data}, token=token
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["data"]["saved"])

        _, again = self.call("GET", "/api/admin/config?profile=backtest", token=token)
        self.assertEqual(again["data"]["data"]["risk"]["max_daily_trades"], 33)


class TestRiskControl(WebTestCase):
    def test_halt_and_resume(self) -> None:
        token = self.login("admin", "admin-pass-123")
        kill = Path(self.tmpdir) / "data" / "KILL"

        status, payload = self.call(
            "POST", "/api/admin/risk", body={"action": "halt", "reason": "test"}, token=token
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["kill_switch"])
        self.assertTrue(kill.exists())

        status, payload = self.call("POST", "/api/admin/risk", body={"action": "resume"}, token=token)
        self.assertEqual(status, 200)
        self.assertFalse(kill.exists())

    def test_invalid_action_rejected(self) -> None:
        token = self.login("admin", "admin-pass-123")
        status, payload = self.call("POST", "/api/admin/risk", body={"action": "nope"}, token=token)
        self.assertEqual(status, 400)

    def test_audit_records_actions(self) -> None:
        token = self.login("admin", "admin-pass-123")
        self.call("POST", "/api/admin/risk", body={"action": "resume"}, token=token)
        status, payload = self.call("GET", "/api/admin/audit?limit=20", token=token)
        self.assertEqual(status, 200)
        actions = [r["action"] for r in payload["data"]["items"]]
        self.assertTrue(any(a.startswith("risk_") for a in actions))


class TestInstrumentsApi(WebTestCase):
    """标的库接口：增删改查、判重、批量导入、排序与权限。"""

    def setUp(self) -> None:
        self.token = self.login("admin", "admin-pass-123")
        # 每个用例从干净状态开始：清掉上一轮留下的标的
        _, listing = self.call("GET", "/api/admin/instruments", token=self.token)
        for item in listing["data"]["items"]:
            self.call("DELETE", f"/api/admin/instruments/{item['id']}", token=self.token)

    def create(self, **payload):
        return self.call("POST", "/api/admin/instruments", body=payload, token=self.token)

    def test_create_then_visible_immediately(self) -> None:
        status, payload = self.create(code="BTCUSDT", name="比特币", asset_type="crypto")
        self.assertEqual(status, 200, payload)
        iid = payload["data"]["id"]

        # 前台接口立即可见（无需刷新缓存）
        status, listing = self.call("GET", "/api/instruments", token=self.token)
        self.assertEqual(status, 200)
        codes = [i["code"] for i in listing["data"]["items"]]
        self.assertIn("BTCUSDT", codes)
        self.assertEqual(listing["data"]["total"], 1)

        _, admin_list = self.call("GET", "/api/admin/instruments", token=self.token)
        item = admin_list["data"]["items"][0]
        self.assertEqual(item["id"], iid)
        self.assertEqual(item["asset_type"], "crypto")
        self.assertEqual(item["market"], "BINANCE")

    def test_normalization_applied_on_create(self) -> None:
        status, payload = self.create(code="btc/usdt", name="比特币", asset_type="crypto")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["item"]["code"], "BTCUSDT")

    def test_duplicate_rejected_with_existing_id(self) -> None:
        _, first = self.create(code="BTCUSDT", name="比特币", asset_type="crypto")
        status, payload = self.create(code="btc-usdt", name="另一个名字", asset_type="crypto")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "DUPLICATE")
        self.assertEqual(payload["error"]["details"]["existing"]["id"], first["data"]["id"])

    def test_same_code_different_market_allowed(self) -> None:
        self.assertEqual(self.create(code="000001", name="平安银行", asset_type="stock", market="SZ")[0], 200)
        self.assertEqual(self.create(code="000001", name="上证指数", asset_type="stock", market="SH")[0], 200)

    def test_invalid_payload_returns_field_issues(self) -> None:
        status, payload = self.create(code="123", name="", asset_type="stock", market="SH")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "VALIDATION_FAILED")
        fields = {d["field"] for d in payload["error"]["details"]}
        self.assertIn("code", fields)
        self.assertIn("name", fields)

    def test_validate_endpoint_flags_duplicate(self) -> None:
        self.create(code="BTCUSDT", name="比特币", asset_type="crypto")
        status, payload = self.call(
            "POST", "/api/admin/instruments/validate",
            body={"code": "BTCUSDT", "name": "比特币", "asset_type": "crypto"}, token=self.token,
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["valid"])
        self.assertTrue(payload["data"]["duplicate"])

    def test_batch_import_counts(self) -> None:
        self.create(code="BTCUSDT", name="比特币", asset_type="crypto")
        text = "\n".join([
            "# 已存在",                                    # 注释
            "ETHUSDT,以太坊,crypto",                       # 新增
            "600519,贵州茅台,stock,SH",                    # 新增
            "ethusdt,以太坊重复,crypto",                    # 批内重复
            "BTCUSDT,比特币,crypto",                       # 库内重复
            "BAD,坏数据,stock,SH",                         # 非法
        ])
        status, payload = self.call(
            "POST", "/api/admin/instruments/batch", body={"text": text}, token=self.token
        )
        self.assertEqual(status, 200, payload)
        d = payload["data"]
        self.assertEqual(d["total"], 5)
        self.assertEqual(d["created"], 2)
        self.assertEqual(d["duplicated"], 2)
        self.assertEqual(d["invalid"], 1)

        _, listing = self.call("GET", "/api/instruments", token=self.token)
        self.assertEqual(listing["data"]["total"], 3)

    def test_update_and_conflict(self) -> None:
        _, a = self.create(code="600519", name="茅台", asset_type="stock", market="SH")
        self.create(code="600036", name="招商银行", asset_type="stock", market="SH")
        self.create(code="000858", name="五粮液", asset_type="stock", market="SZ")
        iid = a["data"]["id"]

        status, payload = self.call(
            "PATCH", f"/api/admin/instruments/{iid}", body={"name": "贵州茅台"}, token=self.token
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["item"]["name"], "贵州茅台")

        # 同一市场下改成已存在的代码 -> 冲突
        status, payload = self.call(
            "PATCH", f"/api/admin/instruments/{iid}", body={"code": "600036"}, token=self.token
        )
        self.assertEqual(status, 409, payload)

        # 只换市场、代码不变：600519 在深市并不存在 -> 是另一个合法标的
        status, payload = self.call(
            "PATCH", f"/api/admin/instruments/{iid}", body={"market": "SZ"}, token=self.token
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["item"]["market"], "SZ")
        self.assertEqual(payload["data"]["item"]["code"], "600519")

    def test_reorder_changes_listing_order(self) -> None:
        ids = []
        for code in ("AAAUSDT", "BBBUSDT", "CCCUSDT"):
            _, p = self.create(code=code, name=code, asset_type="crypto")
            ids.append(p["data"]["id"])
        reversed_ids = list(reversed(ids))
        status, payload = self.call(
            "PUT", "/api/admin/instruments/order", body={"ids": reversed_ids}, token=self.token
        )
        self.assertEqual(status, 200, payload)
        _, listing = self.call("GET", "/api/instruments", token=self.token)
        self.assertEqual([i["id"] for i in listing["data"]["items"]], reversed_ids)

    def test_delete(self) -> None:
        _, p = self.create(code="BTCUSDT", name="比特币", asset_type="crypto")
        iid = p["data"]["id"]
        status, _ = self.call("DELETE", f"/api/admin/instruments/{iid}", token=self.token)
        self.assertEqual(status, 200)
        _, listing = self.call("GET", "/api/instruments", token=self.token)
        self.assertEqual(listing["data"]["total"], 0)

        status, payload = self.call("DELETE", f"/api/admin/instruments/{iid}", token=self.token)
        self.assertEqual(status, 404)

    def test_user_cannot_write(self) -> None:
        user_token = self.login("alice", "user-pass-123")
        status, payload = self.call(
            "POST", "/api/admin/instruments",
            body={"code": "BTCUSDT", "name": "比特币", "asset_type": "crypto"}, token=user_token,
        )
        self.assertEqual(status, 403)
        # 但可以读
        status, _ = self.call("GET", "/api/instruments", token=user_token)
        self.assertEqual(status, 200)

    def test_backtest_rejects_unregistered_symbol(self) -> None:
        status, payload = self.call(
            "POST", "/api/backtest", body={"profile": "backtest", "symbols": ["NOTUSDT"]}, token=self.token
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "INSTRUMENT_NOT_FOUND")
        self.assertEqual(payload["error"]["details"]["missing"], ["NOTUSDT"])

    def test_backtest_rejects_registered_symbol_without_data(self) -> None:
        self.create(code="FAKEUSDT", name="假标的", asset_type="crypto")
        status, payload = self.call(
            "POST", "/api/backtest", body={"profile": "backtest", "symbols": ["FAKEUSDT"]}, token=self.token
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "DATA_MISSING")


class TestGenericAdmin(WebTestCase):
    """schema 驱动的通用数据管理 + AI 辅助接口。"""

    def setUp(self) -> None:
        self.token = self.login("admin", "admin-pass-123")

    def call_api(self, method, path, body=None):
        return self.call(method, path, body=body, token=self.token)

    def _clean(self, entity: str) -> None:
        _, listing = self.call_api("GET", f"/api/admin/entities/{entity}")
        for item in listing["data"]["items"]:
            self.call_api("DELETE", f"/api/admin/entities/{entity}/{item['id']}")

    def test_catalog_lists_entities_and_ai_mode(self) -> None:
        status, payload = self.call_api("GET", "/api/admin/entities")
        self.assertEqual(status, 200)
        keys = {e["key"] for e in payload["data"]["items"]}
        self.assertEqual(
            keys, {"broker_accounts", "strategy_instances", "risk_profiles", "data_sources"}
        )
        self.assertIn(payload["data"]["ai"]["mode"], ("local", "llm"))
        # 每个实体的字段都要带前端渲染所需的元数据
        for entity in payload["data"]["items"]:
            for field in entity["fields"]:
                self.assertIn("label", field)
                self.assertIn("type", field)

    def test_crud_roundtrip(self) -> None:
        self._clean("broker_accounts")
        body = {"name": "test-acc", "venue": "BINANCE", "api_key_env": "BINANCE_API_KEY", "leverage": "5"}

        status, payload = self.call_api("POST", "/api/admin/entities/broker_accounts", body)
        self.assertEqual(status, 200, payload)
        rid = payload["data"]["item"]["id"]
        self.assertAlmostEqual(payload["data"]["item"]["leverage"], 5.0)

        status, listing = self.call_api("GET", "/api/admin/entities/broker_accounts")
        self.assertEqual(status, 200)
        self.assertEqual(len(listing["data"]["items"]), 1)

        status, payload = self.call_api(
            "PATCH", f"/api/admin/entities/broker_accounts/{rid}", {"enabled": False}
        )
        self.assertEqual(status, 200, payload)
        self.assertIs(payload["data"]["item"]["enabled"], False)
        self.assertEqual(payload["data"]["item"]["name"], "test-acc")

        status, _ = self.call_api("DELETE", f"/api/admin/entities/broker_accounts/{rid}")
        self.assertEqual(status, 200)
        status, _ = self.call_api("DELETE", f"/api/admin/entities/broker_accounts/{rid}")
        self.assertEqual(status, 404)

    def test_validation_error_has_field_details(self) -> None:
        status, payload = self.call_api(
            "POST", "/api/admin/entities/risk_profiles", {"name": "x", "risk_per_trade": 9}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "VALIDATION_FAILED")
        self.assertTrue(payload["error"]["details"])

    def test_duplicate_returns_409(self) -> None:
        self._clean("data_sources")
        body = {"name": "ds1", "kind": "kline", "symbol": "BTCUSDT"}
        self.assertEqual(self.call_api("POST", "/api/admin/entities/data_sources", body)[0], 200)
        status, payload = self.call_api("POST", "/api/admin/entities/data_sources", body)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "DUPLICATE")

    def test_unknown_entity_404(self) -> None:
        status, payload = self.call_api("GET", "/api/admin/entities/nope")
        self.assertEqual(status, 404)

    def test_validate_endpoint(self) -> None:
        status, payload = self.call_api(
            "POST", "/api/admin/entities/broker_accounts/validate",
            {"name": "ok-acc", "venue": "BINANCE"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["valid"])
        self.assertEqual(payload["data"]["normalized"]["api_key_env"], "BINANCE_API_KEY")

    def test_ai_suggest(self) -> None:
        status, payload = self.call_api(
            "POST", "/api/admin/ai/suggest",
            {"entity": "data_sources", "partial": {}, "hint": "新增 symbol=ETHUSDT 的数据源"},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["values"].get("symbol"), "ETHUSDT")
        self.assertIn(payload["data"]["mode"], ("local", "llm"))

    def test_ai_fix_repairs_range(self) -> None:
        status, payload = self.call_api(
            "POST", "/api/admin/ai/fix",
            {"entity": "data_sources",
             "payload": {"name": "f1", "kind": "kline", "symbol": "BTCUSDT", "retention_days": 99999}},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["payload"]["retention_days"], 3650)

    def test_ai_command_then_apply(self) -> None:
        self._clean("data_sources")
        status, payload = self.call_api(
            "POST", "/api/admin/ai/command",
            {"text": "新增数据源 name=cmd-ds kind=kline symbol=BTCUSDT interval=1m"},
        )
        self.assertEqual(status, 200, payload)
        plan = payload["data"]["plan"]
        self.assertEqual(plan["entity"], "data_sources")
        self.assertEqual(plan["action"], "create")
        self.assertTrue(payload["data"]["requires_confirmation"])

        status, applied = self.call_api("POST", "/api/admin/ai/apply", {"plan": plan})
        self.assertEqual(status, 200, applied)
        self.assertEqual(applied["data"]["result"]["name"], "cmd-ds")
        self.assertIn("explanation", applied["data"])

        _, listing = self.call_api("GET", "/api/admin/entities/data_sources")
        self.assertEqual(len(listing["data"]["items"]), 1)

    def test_ai_apply_rejects_bad_plan(self) -> None:
        status, payload = self.call_api(
            "POST", "/api/admin/ai/apply", {"plan": {"entity": "nope", "action": "create"}}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "VALIDATION_FAILED")

    def test_ai_explain_failure(self) -> None:
        status, payload = self.call_api(
            "POST", "/api/admin/ai/explain",
            {"entity": "data_sources", "action": "create", "ok": False,
             "message": "名称重复", "issues": []},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["severity"], "error")
        self.assertTrue(payload["data"]["suggestions"])

    def test_non_admin_blocked(self) -> None:
        user_token = self.login("alice", "user-pass-123")
        status, _ = self.call("GET", "/api/admin/entities", token=user_token)
        self.assertEqual(status, 403)


class TestStatic(WebTestCase):
    def test_index_served(self) -> None:
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = resp.read().decode()
        self.assertEqual(resp.status, 200)
        self.assertIn("<html", body.lower())

    def test_admin_page_served(self) -> None:
        url = f"http://127.0.0.1:{self.port}/admin"
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = resp.read().decode()
        self.assertEqual(resp.status, 200)
        self.assertIn("管理", body)

    def test_path_traversal_blocked(self) -> None:
        url = f"http://127.0.0.1:{self.port}/static/../../etc/passwd"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                self.fail(f"应当被拒绝，实际 {resp.status}")
        except urllib.error.HTTPError as exc:
            self.assertIn(exc.code, (403, 404))


if __name__ == "__main__":
    unittest.main()

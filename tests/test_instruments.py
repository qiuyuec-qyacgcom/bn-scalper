"""标的库：跨资产类型的代码校验、规范化与批量解析。

这里的用例把"哪些代码算合法"钉死——因为它是用户输入的第一道闸门，
放过一个错码，后续回测就会拿一个根本不存在的标的去安静地跑出 0 笔交易。
"""

from __future__ import annotations

import unittest

from scalpx.web.instruments import (
    ASSET_TYPES,
    canonical_key,
    normalize_code,
    parse_batch_text,
    type_options,
    validate_instrument,
)


def check(**payload):
    return validate_instrument(payload)


class TestNormalization(unittest.TestCase):
    def test_crypto_separators_removed(self) -> None:
        for raw in ("BTC/USDT", "btc-usdt", " btc_usdt ", "BTCUSDT"):
            self.assertEqual(normalize_code(raw, "crypto"), "BTCUSDT")

    def test_leading_zeros_preserved(self) -> None:
        self.assertEqual(normalize_code("00700", "stock"), "00700")
        self.assertEqual(normalize_code("000001", "stock"), "000001")

    def test_us_class_suffix_kept(self) -> None:
        self.assertEqual(normalize_code("brk.a", "stock"), "BRK.A")

    def test_canonical_key_is_type_scoped(self) -> None:
        # 同一代码在不同市场是两个标的；同一市场下才是同一个
        self.assertNotEqual(
            canonical_key("stock", "SH", "000001"), canonical_key("stock", "SZ", "000001")
        )
        self.assertEqual(canonical_key("crypto", "BINANCE", "btcusdt"), "crypto:BINANCE:BTCUSDT")


class TestValidationPerType(unittest.TestCase):
    def test_crypto(self) -> None:
        self.assertTrue(check(code="BTCUSDT", name="比特币", asset_type="crypto").ok)
        self.assertFalse(check(code="BTC", name="比特币", asset_type="crypto").ok)
        self.assertFalse(check(code="BTCUSDT", name="比特币", asset_type="crypto", market="NASDAQ").ok)

    def test_stock_cn(self) -> None:
        r = check(code="600519", name="贵州茅台", asset_type="stock", market="SH")
        self.assertTrue(r.ok, r.issues)
        self.assertEqual(r.data["currency"], "CNY")
        self.assertFalse(check(code="60051", name="短码", asset_type="stock", market="SH").ok)
        self.assertFalse(check(code="60051A", name="混码", asset_type="stock", market="SZ").ok)

    def test_stock_hk_and_us(self) -> None:
        self.assertTrue(check(code="00700", name="腾讯控股", asset_type="stock", market="HK").ok)
        self.assertFalse(check(code="700", name="腾讯控股", asset_type="stock", market="HK").ok)
        self.assertTrue(check(code="AAPL", name="Apple", asset_type="stock", market="US").ok)
        self.assertTrue(check(code="BRK.A", name="伯克希尔", asset_type="stock", market="US").ok)
        self.assertFalse(check(code="TOOLONGTICKER", name="x", asset_type="stock", market="US").ok)

    def test_fund_and_bond(self) -> None:
        self.assertTrue(check(code="510300", name="沪深300ETF", asset_type="fund").ok)
        self.assertTrue(check(code="019547", name="国债", asset_type="bond").ok)
        self.assertFalse(check(code="ABC123", name="乱码", asset_type="fund", market="CN").ok)

    def test_futures_cn_and_us(self) -> None:
        self.assertTrue(check(code="RB2410", name="螺纹钢", asset_type="futures", market="SHFE").ok)
        self.assertTrue(check(code="IF2409", name="沪深300期货", asset_type="futures", market="CFFEX").ok)
        self.assertTrue(check(code="ESZ4", name="标普期货", asset_type="futures", market="CME").ok)
        self.assertFalse(check(code="RB", name="无月份", asset_type="futures", market="SHFE").ok)

    def test_forex(self) -> None:
        r = check(code="EUR/USD", name="欧元美元", asset_type="forex")
        self.assertTrue(r.ok, r.issues)
        self.assertEqual(r.data["code"], "EURUSD")
        self.assertFalse(check(code="EURO", name="三位", asset_type="forex").ok)

    def test_unknown_type_rejected(self) -> None:
        r = check(code="BTCUSDT", name="x", asset_type="warrant")
        self.assertFalse(r.ok)
        self.assertTrue(any(i.field == "asset_type" for i in r.issues))


class TestFieldValidation(unittest.TestCase):
    def test_name_required_and_bounded(self) -> None:
        self.assertFalse(check(code="BTCUSDT", name="", asset_type="crypto").ok)
        self.assertFalse(check(code="BTCUSDT", name="x" * 65, asset_type="crypto").ok)

    def test_numeric_bounds(self) -> None:
        r = check(code="BTCUSDT", name="BTC", asset_type="crypto", price_tick=0)
        self.assertFalse(r.ok)
        self.assertEqual(r.issues[0].field, "price_tick")

        r2 = check(code="BTCUSDT", name="BTC", asset_type="crypto", qty_step="abc")
        self.assertFalse(r2.ok)

        r3 = check(code="BTCUSDT", name="BTC", asset_type="crypto", price_tick="0.5", qty_step="0.001")
        self.assertTrue(r3.ok, r3.issues)
        self.assertAlmostEqual(r3.data["price_tick"], 0.5)
        self.assertAlmostEqual(r3.data["qty_step"], 0.001)

    def test_currency_override(self) -> None:
        r = check(code="600519", name="茅台", asset_type="stock", market="SH", currency="usd")
        self.assertTrue(r.ok)
        self.assertEqual(r.data["currency"], "USD")
        self.assertFalse(check(code="600519", name="茅台", asset_type="stock", currency="RMB!").ok)

    def test_meta_is_dict(self) -> None:
        r = check(code="BTCUSDT", name="BTC", asset_type="crypto", meta='{"leverage": 3}')
        self.assertTrue(r.ok, r.issues)
        self.assertEqual(r.data["meta"]["leverage"], 3)
        self.assertFalse(check(code="BTCUSDT", name="BTC", asset_type="crypto", meta="[1,2]").ok)

    def test_defaults_applied(self) -> None:
        r = check(code="BTCUSDT", name="BTC", asset_type="crypto")
        self.assertTrue(r.ok)
        self.assertEqual(r.data["market"], "BINANCE")
        self.assertEqual(r.data["currency"], "USDT")
        self.assertTrue(r.data["is_active"])
        self.assertAlmostEqual(r.data["price_tick"], 0.01)


class TestBatchParse(unittest.TestCase):
    def test_comma(self) -> None:
        rows = parse_batch_text("BTCUSDT,比特币,crypto, BINANCE")
        self.assertEqual(rows[0]["code"], "BTCUSDT")
        self.assertEqual(rows[0]["name"], "比特币")
        self.assertEqual(rows[0]["asset_type"], "crypto")
        self.assertEqual(rows[0]["market"], "BINANCE")

    def test_tab_and_spaces(self) -> None:
        self.assertEqual(len(parse_batch_text("A,甲,crypto\nB\t乙\tstock\tSH")), 2)
        rows = parse_batch_text("600519 贵州茅台  stock  SH")
        self.assertEqual(rows[0]["market"], "SH")

    def test_comments_and_blank_lines_skipped(self) -> None:
        text = """
        # 这是注释
        BTCUSDT,比特币,crypto

        600519,茅台,stock,SH
        """
        self.assertEqual(len(parse_batch_text(text)), 2)

    def test_optional_numeric_columns(self) -> None:
        rows = parse_batch_text("AAPL,苹果,stock,US,USD,0.01,1")
        self.assertEqual(rows[0]["price_tick"], "0.01")
        self.assertEqual(rows[0]["qty_step"], "1")

    def test_header_line_treated_as_row_and_rejected_later(self) -> None:
        # 不做表头猜测：宁可让用户看到一条 invalid，也不要静默丢数据
        rows = parse_batch_text("code,name,asset_type\nBTCUSDT,比特币,crypto")
        self.assertEqual(len(rows), 2)


class TestTypeOptions(unittest.TestCase):
    def test_every_type_exposed(self) -> None:
        options = type_options()
        self.assertEqual(len(options), len(ASSET_TYPES))
        for opt in options:
            self.assertTrue(opt["markets"])
            for m in opt["markets"]:
                self.assertTrue(m["pattern"].startswith("^"))
                self.assertTrue(m["hint"])


if __name__ == "__main__":
    unittest.main()

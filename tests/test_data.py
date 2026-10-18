import unittest
from unittest.mock import patch
import json
import urllib.error

from fullgoal_quant.common import GuardError
from fullgoal_quant.data import (parse_nav, parse_status_page, is_otc, is_otc_candidate,
                                js_json, get_text)
from fullgoal_quant.telegram import api, chunks


class DataTests(unittest.TestCase):
    def nav(self, points):
        return 'var ishb=false;var fS_code="000001";var fS_name="测试";var Data_netWorthTrend=' + json.dumps(points) + ';'

    def test_dividend_total_return(self):
        _, f = parse_nav(self.nav([{"x": 1704153600000, "y": 1, "equityReturn": 0},
                                  {"x": 1704240000000, "y": .9, "equityReturn": 0, "unitMoney": "分红"}]), "000001")
        self.assertEqual(f.tri.iloc[-1], 1)
        self.assertEqual(f.unit_nav.iloc[-1], .9)

    def test_corrupt_nav_rejected(self):
        with self.assertRaises(GuardError):
            parse_nav(self.nav([{"x": 1, "y": 1, "equityReturn": 0}] * 2), "000001")

    def test_status_limit(self):
        x = parse_status_page('交易状态：<span>限大额 单日累计购买上限2.00万元</span><span>开放赎回</span></div>')
        self.assertEqual(x["purchase_limit"], 20000)
        self.assertTrue(x["buy_open"])
        self.assertFalse(parse_status_page('交易状态：暂停申购 开放赎回</div>')["buy_open"])
        self.assertFalse(parse_status_page('交易状态：限大额 开放赎回</div>')["buy_open"])

    def test_no_eval(self):
        with self.assertRaises(GuardError):
            js_json('var r=alert("x");', "r")

    def test_connection_reset_is_bounded_guard_error(self):
        with patch("urllib.request.urlopen", side_effect=ConnectionResetError("reset")), \
             patch("fullgoal_quant.data.time.sleep"):
            with self.assertRaises(GuardError):
                get_text("https://example.invalid/", attempts=2)

    def test_candidate(self):
        self.assertFalse(is_otc_candidate("富国某ETF", "股票型"))
        self.assertTrue(is_otc_candidate("富国某ETF联接A", "指数型"))
        self.assertFalse(is_otc_candidate("富国三年持有", "混合型"))

    def test_otc_venue_is_separate_from_model_support(self):
        # Direct ETFs are on-exchange; their feeder funds are OTC.
        self.assertFalse(is_otc("富国某ETF", "股票型"))
        self.assertTrue(is_otc("富国某ETF联接A", "指数型"))
        # Lock-up and money funds are OTC even though the model cannot trade them.
        self.assertTrue(is_otc("富国三年持有", "混合型"))
        self.assertFalse(is_otc_candidate("富国三年持有", "混合型"))
        self.assertTrue(is_otc("富国测试货币A", "货币型-普通货币"))
        self.assertFalse(is_otc_candidate("富国测试货币A", "货币型-普通货币"))
        self.assertFalse(is_otc("华夏成长混合", "混合型"))

    def test_secret_never_in_error(self):
        secret = "123456:SENSITIVE"
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError(secret)):
            with self.assertRaises(GuardError) as caught:
                api("getMe", {}, token=secret)
        self.assertNotIn(secret, str(caught.exception))

    def test_unicode_chunk(self):
        s = "😀测试" * 3000
        parts = list(chunks(s))
        self.assertEqual("".join(parts), s)
        self.assertTrue(all(len(p.encode("utf-16-le")) // 2 <= 3000 for p in parts))


if __name__ == "__main__":
    unittest.main()

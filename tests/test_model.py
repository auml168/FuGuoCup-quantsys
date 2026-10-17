import copy
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from fullgoal_quant.backtest import simulate, metrics, compare
from fullgoal_quant.calendar import Calendar
from fullgoal_quant.common import GuardError, load_config, redemption_fee
from fullgoal_quant.common import write_json
from fullgoal_quant.signals import target_weights


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.meta = {x["code"]: x for x in self.cfg["research_funds"]}
        days = pd.bdate_range("2021-01-01", periods=800)
        # Synthetic fixtures test accounting mechanics, never report fund performance.
        self.p = pd.DataFrame({c: np.exp(np.arange(800) * (.001 + j * .0001)) for j, c in enumerate(self.meta)}, index=days)

    def test_weight_caps(self):
        for strategy in ("equal_weight", "momentum", "relative_strength", "sector_rotation", "timing"):
            w, _ = target_weights(self.p.iloc[:200], strategy, self.cfg, self.meta)
            self.assertLessEqual(sum(w.values()), .95 + 1e-9)
            self.assertTrue(all(x <= .35 for x in w.values()))
            for sector in set(x["sector"] for x in self.meta.values()):
                self.assertLessEqual(sum(v for c, v in w.items() if self.meta[c]["sector"] == sector), .5 + 1e-9)

    def test_future_suffix_cannot_change_prefix(self):
        start, middle, end = self.p.index[150], self.p.index[200], self.p.index[250]
        _, before, trades1 = simulate(self.p, self.cfg, self.meta, "momentum", start, end)
        altered = self.p.copy()
        altered.loc[altered.index > middle] *= 100
        _, after, trades2 = simulate(altered, self.cfg, self.meta, "momentum", start, end)
        pd.testing.assert_frame_equal(before[before.date <= str(middle.date())], after[after.date <= str(middle.date())])
        self.assertEqual([t for t in trades1 if t["date"] <= str(middle.date())], [t for t in trades2 if t["date"] <= str(middle.date())])
        for trade in trades1:
            self.assertGreaterEqual(self.p.index.get_loc(pd.Timestamp(trade["date"])) - self.p.index.get_loc(pd.Timestamp(trade["signal_date"])), 2)

    def test_missing_price_no_fill(self):
        p = self.p.copy()
        p.iloc[150, :] = np.nan
        _, _, trades = simulate(p, self.cfg, self.meta, "momentum", p.index[150], p.index[150])
        self.assertEqual(trades, [])

    def test_fees_and_cash_no_leverage(self):
        stat, curve, trades = simulate(self.p, self.cfg, self.meta, "momentum", self.p.index[150], self.p.index[250])
        self.assertGreater(stat["fees"], 0)
        self.assertTrue((curve.cash >= -1e-8).all())
        self.assertGreater(len(trades), 0)
        with tempfile.TemporaryDirectory() as out:
            write_json(Path(out) / 'trades.json', trades)

    def test_buy_budget_independent_of_execution_nav(self):
        start = self.p.index[150]
        _, _, t1 = simulate(self.p, self.cfg, self.meta, 'equal_weight', start, start)
        altered = self.p.copy()
        altered.loc[start] *= 2
        _, _, t2 = simulate(altered, self.cfg, self.meta, 'equal_weight', start, start)
        self.assertEqual([t['gross'] for t in t1], [t['gross'] for t in t2])

    def test_settlement_restricts_reinvestment(self):
        code1, code2 = list(self.meta)[:2]
        cfg = copy.deepcopy(self.cfg)
        cfg["rules"].update(min_hold_days=0, rebalance_sessions=1, max_fund_weight=1, max_sector_weight=1, max_exposure=1)
        calls = 0
        def weights(*args):
            nonlocal calls
            calls += 1
            return ({code1: 1} if calls == 1 else {code2: 1}), pd.DataFrame()
        p = self.p.loc[:, [code1, code2]] * 0 + 1
        with patch("fullgoal_quant.backtest.target_weights", side_effect=weights):
            _, curve, trades = simulate(p, cfg, self.meta, "momentum", p.index[150], p.index[156])
        sell = next(t for t in trades if t["side"] == "sell")
        buy2 = next(t for t in trades if t["side"] == "buy" and t["code"] == code2)
        self.assertGreaterEqual(p.index.get_loc(pd.Timestamp(buy2["date"])) - p.index.get_loc(pd.Timestamp(sell["date"])), 3)
        self.assertTrue((curve.receivable > 0).any())

    def test_short_holding_fee_and_initial_drawdown(self):
        self.assertEqual(redemption_fee(self.cfg["rules"], 6), .015)
        self.assertEqual(redemption_fee(self.cfg["rules"], 7), .005)
        self.assertAlmostEqual(metrics([99, 100], 100)["max_drawdown"], .01)

    def test_test_period_cannot_choose_strategy(self):
        frames = {c: pd.DataFrame({"tri": self.p[c]}) for c in self.p}
        def fake(prices, cfg, meta, strategy, start, end, *rest):
            # Validation favors cash; holdout favors momentum. Cash must stay selected.
            is_test = start == self.p.index[-126]
            ret = (9 if strategy == "momentum" else 0) if is_test else (1 if strategy == "cash" else 0)
            return dict(total_return=ret, max_drawdown=0, sharpe=0, final_assets=200000, fees=0), pd.DataFrame(), []
        with tempfile.TemporaryDirectory() as out, patch("fullgoal_quant.backtest.simulate", side_effect=fake):
            result = compare(frames, self.cfg, self.meta, out)
        self.assertEqual(result["selected"], "cash")

    def test_calendar_holiday_and_out_of_range(self):
        cal = Calendar()
        self.assertFalse(cal.is_session(date(2026, 10, 1)))
        self.assertFalse(cal.is_session(date(2026, 10, 10)))
        self.assertTrue(cal.is_session(date(2026, 10, 19)))
        self.assertEqual(cal.previous(date(2026, 10, 8)), date(2026, 9, 30))
        with self.assertRaises(GuardError):
            cal.is_session(date(2027, 1, 4))


if __name__ == "__main__":
    unittest.main()

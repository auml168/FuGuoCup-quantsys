import copy
import importlib.util
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from fullgoal_quant.cli import main, slot_guard, historical_status
from fullgoal_quant.common import TZ, fingerprint, load_config, write_json, read_json, GuardError
from fullgoal_quant.portfolio import propose, import_account
from fullgoal_quant.report import build_report


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.code = self.cfg['research_funds'][0]['code']
        self.today = date(2026, 10, 19)
        self.account = dict(as_of=str(self.today), cash=200000, lots=[], pending=[])
        self.status = {self.code: dict(buy_open=True, sell_open=True, purchase_limit=20000)}

    def test_cash_and_subscription_limit(self):
        r = propose(self.account, {self.code: .35}, {self.code: 1}, self.status, self.cfg, self.today, [self.code])
        self.assertEqual(r['orders'][0]['amount'], 20000)
        self.assertEqual(r['cash_after_buys'], 180000)

    def test_suspension_and_pending(self):
        self.status[self.code]['buy_open'] = False
        r = propose(self.account, {self.code: .3}, {self.code: 1}, self.status, self.cfg, self.today, [self.code])
        self.assertEqual(r['orders'][0]['amount'], 0)
        self.account['pending'] = [dict(type='sell', amount=1000, settle_on='2026-10-20')]
        r = propose(self.account, {}, {}, {}, self.cfg, self.today, [self.code])
        self.assertTrue(r['errors'])
        self.assertFalse(r['orders'])

    def test_no_unconfirmed_sale(self):
        self.account['cash'] = 0
        self.account['lots'] = [dict(code=self.code, units=100, confirmed_on='2026-10-16')]
        r = propose(self.account, {}, {self.code: 1}, self.status, self.cfg, self.today, [self.code])
        self.assertEqual(r['orders'][0]['action'], '暂缓卖出')
        self.assertEqual(r['orders'][0]['amount'], 0)

    def test_actual_units_fee_and_no_cash_reuse(self):
        self.account.update(cash=100, lots=[dict(code=self.code, units=10000, confirmed_on='2026-10-01')])
        second = self.cfg['research_funds'][1]['code']
        self.status[second] = dict(buy_open=True, sell_open=True, purchase_limit=None)
        r = propose(self.account, {second: .35}, {self.code: 2, second: 1}, self.status, self.cfg, self.today, [self.code, second])
        sale = next(o for o in r['orders'] if o['action'] == '卖出')
        buy = next(o for o in r['orders'] if o['code'] == second)
        self.assertEqual(sale['units'], 10000)
        self.assertEqual(sale['fee_estimate'], 100)
        self.assertLessEqual(buy['amount'], 100)

    def test_stale_and_unknown_position(self):
        self.account['as_of'] = '2026-10-01'
        self.account['lots'] = [dict(code='999999', units=100, confirmed_on='2026-10-01')]
        r = propose(self.account, {}, {}, {}, self.cfg, self.today, [self.code])
        self.assertGreaterEqual(len(r['errors']), 2)
        self.assertFalse(r['orders'])

    def test_import_keeps_first_backup(self):
        with tempfile.TemporaryDirectory() as d:
            src, dest = Path(d)/'new.json', Path(d)/'account.json'
            write_json(src, self.account)
            write_json(dest, {'old': True})
            import_account(src, dest)
            self.assertEqual(read_json(str(dest)+'.backup'), {'old': True})
            second = {**self.account, 'cash': 123456}
            write_json(src, second)
            import_account(src, dest)
            self.assertEqual(read_json(str(dest)+'.backup'), {'old': True})
            self.assertEqual(read_json(dest)['cash'], 123456)
            self.assertTrue(any(Path(d).glob('account.json.backup-*')))

    def test_account_validate_hides_amounts_in_actions(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'account.json'
            write_json(path, self.account)
            buffer = io.StringIO()
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), redirect_stdout(buffer):
                self.assertEqual(main(['account', 'validate', '--file', str(path)]), 0)
            self.assertNotIn('200000', buffer.getvalue())
            self.assertIn('公开日志', buffer.getvalue())

    def test_slot_boundaries(self):
        self.assertTrue(slot_guard('afternoon', datetime(2026,10,19,14,20,tzinfo=TZ)))
        self.assertFalse(slot_guard('afternoon', datetime(2026,10,19,15,1,tzinfo=TZ)))
        self.assertFalse(slot_guard('morning', datetime(2026,10,19,8,29,tzinfo=TZ)))


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.codes = [f['code'] for f in self.cfg['research_funds']]
        self.at = datetime(2026,10,19,14,20,tzinfo=TZ)
        dates = pd.bdate_range('2026-01-01','2026-10-16')
        self.frames = {c: pd.DataFrame({'unit_nav': np.linspace(1,1.2,len(dates)), 'tri': np.linspace(1,1.2,len(dates))},index=dates) for c in self.codes}
        self.snapshot = dict(funds={c: dict(name='Synthetic test fixture', buy_open=True,sell_open=True,purchase_limit=None,fetched_at=self.at.isoformat()) for c in self.codes}, errors={})
        self.selection = dict(config_hash=fingerprint(self.cfg), codes=self.codes, selected='equal_weight',data_end='2026-10-16',historical_status='assumed_open_not_competition_validated',
                              results={'equal_weight': {'validation':dict(total_return=.1),'test':dict(total_return=-.1,max_drawdown=.2,sharpe=-1)}})
        self.account = dict(as_of='2026-10-19',cash=200000,lots=[],pending=[])

    def test_preview_and_no_same_day_nav(self):
        r = build_report(self.cfg,self.snapshot,self.frames,self.selection,self.account,research=True,at=self.at)
        self.assertFalse(r['actionable'])
        self.assertEqual(r['signal_cutoff'], '2026-10-15')
        self.assertIn('研究', r['status'])
        self.assertIn('独立测试亏损',r['text'])

    def test_unverifiable_selected_history_cannot_emit_orders(self):
        self.selection['results']['equal_weight'].update(validation=None,test=None,errors={'development':'净值断点'})
        r=build_report(self.cfg,self.snapshot,self.frames,self.selection,self.account,research=True,at=self.at)
        self.assertFalse(r['actionable'])
        self.assertFalse(r['orders'])
        self.assertIn('无法可靠计价',r['text'])

    def test_formal_gate_does_not_hide_bad_holdout(self):
        self.cfg['competition'].update(verified=True,rules_source='fixture',universe_source='fixture',midpoint_date='2026-11-30',end_date='2026-12-31',allowed_codes=self.codes)
        self.cfg['rules']['verified']=True
        self.cfg['fund_rules']={c:{'verified':True,'source':'fixture'} for c in self.codes}
        self.selection['config_hash']=fingerprint(self.cfg)
        r = build_report(self.cfg,self.snapshot,self.frames,self.selection,self.account,at=self.at)
        self.assertFalse(r['actionable'])
        self.assertFalse(r['orders'])
        self.assertTrue(any('部署风险门槛' in e for e in r['errors']))

    def test_signal_strategy_and_telegram_text(self):
        r = build_report(self.cfg, self.snapshot, self.frames, self.selection,
                         self.account, research=True, at=self.at)
        self.assertEqual(r['signal_strategy'], 'timing')
        self.assertIn('研究信号', r['status'])
        self.assertIn('今日信号', r['text'])
        self.assertTrue(r['signal'])
        self.assertIn('目标组合', r['telegram_text'])
        self.assertIn('timing', r['telegram_text'])
        self.assertIn('研究信号', r['telegram_text'])

    def test_stale_data_stops_amounts(self):
        self.snapshot['funds'][self.codes[0]]['fetched_at']='2026-10-16T10:00:00+08:00'
        r = build_report(self.cfg,self.snapshot,self.frames,self.selection,self.account,research=True,at=self.at)
        self.assertFalse(r['orders'])
        self.assertTrue(r['errors'])

    def test_report_contains_schedule_and_calendar_gap(self):
        r = build_report(self.cfg,self.snapshot,self.frames,self.selection,self.account,research=True,at=self.at)
        self.assertEqual(r['schedule']['phase'], '半程赛')
        self.assertIn('2027-01-15', r['text'])
        self.assertIn('2027-04-30', r['text'])
        self.assertIn('仅核验至 2026-12-31', r['text'])

    def test_final_evening_has_no_next_session_plan(self):
        self.cfg['competition'].update(midpoint_date='2026-10-19', end_date='2026-10-19')
        self.selection['config_hash']=fingerprint(self.cfg)
        r = build_report(self.cfg,self.snapshot,self.frames,self.selection,self.account,research=True,at=self.at.replace(hour=20,minute=30))
        self.assertIsNone(r['target_day'])
        self.assertFalse(r['orders'])
        self.assertFalse(r['rebalance_due'])

    def test_empty_whitelist_and_invalid_calendar(self):
        with self.assertRaises(GuardError):
            build_report(self.cfg,self.snapshot,self.frames,self.selection,self.account,at=self.at)
        with self.assertRaises(GuardError):
            build_report(self.cfg,self.snapshot,self.frames,self.selection,self.account,research=True,at=datetime(2027,1,4,8,30,tzinfo=TZ))

    def test_cli_weekend_skips_network(self):
        with patch('fullgoal_quant.cli.now_cn', return_value=datetime(2026,10,17,8,30,tzinfo=TZ)), patch('fullgoal_quant.cli.Collector.collect') as collect:
            self.assertEqual(main(['run','--research']), 0)
            collect.assert_not_called()

    def test_status_strings_are_not_truthy(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'bad.csv'
            path.write_text('code,date,buy_open,sell_open\n100032,2026-10-16,yes,no\n')
            with self.assertRaises(GuardError):
                historical_status(path)

    def test_missing_schedule_cannot_bypass_time_window(self):
        with patch.dict(os.environ, {'FQ_EVENT':'schedule','FQ_SCHEDULE':''}), patch('fullgoal_quant.cli.Collector.collect') as collect:
            self.assertEqual(main(['run','--slot','auto']), 2)
            collect.assert_not_called()

    def test_secret_bootstrap_rejects_shell_syntax(self):
        spec = importlib.util.spec_from_file_location('setup_local_secrets', Path('scripts/setup_local_secrets.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch('getpass.getpass', side_effect=['123:$(bad)', '123']), patch('os.open') as create:
            with self.assertRaises(SystemExit):
                module.main()
            create.assert_not_called()


if __name__ == '__main__':
    unittest.main()

import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
from datetime import date,datetime
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd

from fullgoal_quant.common import GuardError, load_config, competition_errors, TZ, write_json
from fullgoal_quant.data import parse_nav, parse_profile, Collector, load_market
from fullgoal_quant.universe import (classify, metadata_for, representative_codes,
                                     analysis_codes, active_codes, theme_of)
from fullgoal_quant.signals import (target_weights, CHALLENGERS, breadth_exposure,
                                    theme_watchlist)
from fullgoal_quant.calendar import parse_official_year,extend_official,Calendar
from fullgoal_quant.notifications import reserve,complete,state_at
from fullgoal_quant.portfolio import validate_account,propose
from fullgoal_quant.research import folds,segment,evaluate
from fullgoal_quant.backtest import simulate,metrics,market_matrix,QualityEvents


class UniverseQualityTests(unittest.TestCase):
    def test_classes_grouped_and_special_products_retained(self):
        a=classify('富国测试混合A','混合型-偏股')
        c=classify('富国测试混合C','混合型-偏股')
        self.assertEqual(a['group'],c['group'])
        self.assertFalse(a['competition_approved'])
        self.assertTrue(classify('富国某三年持有A','混合型')['unsupported_reasons'])
        self.assertFalse(classify('华夏成长混合','混合型')['otc_candidate'])
        self.assertEqual(classify('富国纯债A','债券型-长债')['asset_type'],'bond')
        self.assertEqual(classify('富国转债A','债券型-可转债')['asset_type'],'hybrid_bond')
        # Eastmoney's "指数型-固收" is fixed income, not equity.
        self.assertEqual(classify('富国中债1-3年国开债A','指数型-固收')['asset_type'],'bond')
        self.assertFalse(classify('富国上海金ETF联接A','指数型-其他')['otc_candidate'])
        # Venue and model support are independent: the coverage layer keeps
        # lock-up/QDII/FOF/money funds even when the buy model rejects them.
        self.assertTrue(a['otc'])
        self.assertTrue(a['model_supported'])
        self.assertEqual(a['otc_candidate'], a['model_supported'])
        self.assertFalse(classify('富国某ETF','股票型')['otc'])
        self.assertTrue(classify('富国某ETF联接A','指数型')['otc'])
        hold=classify('富国某三年持有A','混合型')
        self.assertTrue(hold['otc'])
        self.assertFalse(hold['model_supported'])
        qdii=classify('富国全球债券(QDII)人民币','QDII-混合债')
        self.assertTrue(qdii['otc'])
        self.assertFalse(qdii['model_supported'])
        self.assertFalse(classify('华夏成长混合','混合型')['identity_verified'])
        self.assertTrue(classify('富国上海金ETF联接A','指数型-其他')['otc'])
        self.assertFalse(classify('富国上海金ETF联接A','指数型-其他')['model_supported'])

    def test_detail_manager_and_risk_not_guessed(self):
        info=parse_profile('<div class="infoOfFund">中高风险</table><span>管 理 人</span>：<a href="https://fund.eastmoney.com/company/80000221.html">富国</a>')
        self.assertEqual(info['risk_label'],'中高风险')
        with self.assertRaises(GuardError):
            parse_profile('<span>管 理 人</span>：<a href="https://fund.eastmoney.com/company/000000.html">其他</a>')

    def test_bad_initial_interval_is_quarantined_not_invented(self):
        points=[dict(x=1704153600000,y=1,equityReturn=0,unitMoney=''),dict(x=1704240000000,y=.9,equityReturn=0,unitMoney=''),dict(x=1704326400000,y=.909,equityReturn=1,unitMoney='')]
        text='var ishb=false;var fS_code="000001";var fS_name="测试";var Data_netWorthTrend='+json.dumps(points)+';'
        _,f=parse_nav(text,'000001')
        self.assertTrue(f.quality_break.iloc[1])
        self.assertTrue(pd.isna(f.tri.iloc[1]))
        self.assertAlmostEqual(f.tri.iloc[-1],1.01)
        self.assertEqual(len(f),3)

    def test_future_quality_event_preserves_all_past_rows(self):
        points=[dict(x=1704153600000+i*86400000,y=1+i*.001,equityReturn=(0 if i==0 else .001/(1+(i-1)*.001)*100),unitMoney='') for i in range(10)]
        def parse(rows):
            return parse_nav('var ishb=false;var fS_code="000001";var fS_name="测试";var Data_netWorthTrend='+json.dumps(rows)+';','000001')[1]
        before=parse(points)
        after=parse(points+[dict(x=1704153600000+10*86400000,y=.5,equityReturn=0,unitMoney='')])
        pd.testing.assert_frame_equal(before,after.iloc[:10])

    def test_unmarked_missing_tri_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'nav').mkdir()
            (root/'nav/100032.csv').write_text('date,unit_nav,daily_return,tri,quality_break\n2026-10-16,1,0,,False\n')
            write_json(root/'snapshot.json',{'funds':{'100032':{}},'errors':{}})
            with self.assertRaises(GuardError):load_market(root)

    def test_tampered_csv_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'nav').mkdir()
            path=root/'nav/100032.csv';path.write_text('date,unit_nav,daily_return,tri\n2026-10-16,1,0,1\n')
            write_json(root/'snapshot.json',{'funds':{'100032':{'csv_sha256':'wrong'}},'errors':{}})
            with self.assertRaises(GuardError):load_market(root)

    def test_offline_rebuild_never_refreshes_old_source_or_extends_cutoff(self):
        path=Path(__file__).resolve().parents[1]/'scripts/rebuild_snapshot.py'
        spec=importlib.util.spec_from_file_location('rebuild_snapshot_test',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        points=[dict(x=1704153600000+i*86400000,y=1,equityReturn=0,unitMoney='') for i in range(3)]
        raw='var ishb=false;var fS_code="123456";var fS_name="富国测试A";var Data_netWorthTrend='+json.dumps(points)+';'
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'nav').mkdir();source=root/'raw.txt';source.write_text(raw)
            meta={'name':'富国测试A','fetched_at':'2024-01-03T20:00:00+08:00','latest_nav_date':'2024-01-03','sources':[{'raw':str(source),'sha256':hashlib.sha256(raw.encode()).hexdigest()}]}
            write_json(root/'snapshot.json',{'mode':'research','fetched_at':meta['fetched_at'],'funds':{'123456':meta}})
            write_json(root/'universe.json',{'funds':[{'code':'123456'}]})
            result=module.rebuild(root,load_config())
            self.assertEqual(result['funds']['123456']['history_rows'],2)
            self.assertEqual(result['fetched_at'],meta['fetched_at'])
            self.assertEqual(result['funds']['123456']['fetched_at'],meta['fetched_at'])
            result['mode']='competition';write_json(root/'snapshot.json',result)
            with self.assertRaises(GuardError):module.rebuild(root,load_config())

    def test_dynamic_collector_not_bound_to_eight(self):
        cfg=load_config()
        catalog={'funds':[{'code':'123456','name':'富国测试混合A','category':'混合型',**classify('富国测试混合A','混合型')}],'sources':[]}
        points=[dict(x=1704153600000,y=1,equityReturn=0),dict(x=1704240000000,y=1.01,equityReturn=1)]
        nav='var ishb=false;var fS_code="123456";var fS_name="富国测试混合A";var Data_netWorthTrend='+json.dumps(points)+';'
        status='交易状态：开放申购 开放赎回</div><span>管 理 人</span>：<a href="https://fund.eastmoney.com/company/80000221.html">富国</a>'
        with tempfile.TemporaryDirectory() as d,patch.object(Collector,'discover',return_value=catalog),patch('fullgoal_quant.data.get_text',side_effect=lambda u:nav if 'pingzhongdata' in u else status):
            s=Collector(d).collect(cfg)
        self.assertEqual(set(s['funds']),{'123456'})
        self.assertFalse(s['funds']['123456']['research_ready'])
        # Two rows are a coverage state, not a collection error.
        self.assertFalse(s['funds']['123456']['analysis_ready'])
        self.assertEqual(s['funds']['123456']['collection_status'], 'insufficient_history')
        self.assertEqual(s['errors'], {})
        self.assertTrue(s['universe_complete'])
        self.assertEqual(s['coverage']['otc'], 1)
        self.assertEqual(s['coverage']['insufficient_history'], 1)

    def test_collect_all_otc_and_expect_money_fund_unsupported(self):
        cfg=load_config()
        normal='123456'
        money='654321'
        catalog={'funds':[
            {'code':normal,'name':'富国测试混合A','category':'混合型',**classify('富国测试混合A','混合型')},
            {'code':money,'name':'富国测试货币A','category':'货币型-普通货币',**classify('富国测试货币A','货币型-普通货币')},
        ],'sources':[]}
        points=[dict(x=1704153600000,y=1,equityReturn=0),dict(x=1704240000000,y=1.01,equityReturn=1)]
        nav='var ishb=false;var fS_code="123456";var fS_name="富国测试混合A";var Data_netWorthTrend='+json.dumps(points)+';'
        status='交易状态：开放申购 开放赎回</div><span>管 理 人</span>：<a href="https://fund.eastmoney.com/company/80000221.html">富国</a>'
        with tempfile.TemporaryDirectory() as d,patch.object(Collector,'discover',return_value=catalog),patch('fullgoal_quant.data.get_text',side_effect=lambda u:nav if 'pingzhongdata' in u else status):
            s=Collector(d).collect(cfg)
            universe=json.loads((Path(d)/'universe.json').read_text())
        self.assertEqual(set(s['funds']),{normal})
        self.assertIn(money,s['expected_unsupported'])
        self.assertNotIn(money,s['errors'])
        self.assertTrue(s['universe_complete'])
        money_row=next(f for f in universe['funds'] if f['code']==money)
        self.assertEqual(money_row['collection_status'],'model_unsupported')
        self.assertTrue(money_row['otc'])
        self.assertFalse(money_row['model_supported'])


    def test_theme_tags_cover_hotspots_and_future_themes(self):
        self.assertEqual(theme_of('富国中证芯片产业ETF联接A','指数型-股票'),'半导体')
        self.assertEqual(theme_of('富国创业板人工智能ETF联接A','指数型-股票'),'人工智能')
        self.assertEqual(theme_of('富国存储测试A','混合型'),'存储')
        self.assertEqual(theme_of('富国军工龙头ETF','指数型-股票'),'军工/航天')
        self.assertEqual(theme_of('富国沪深300增强A','指数型-股票'),'宽基')
        self.assertEqual(theme_of('富国纯债债券A','债券型-长债'),'债券')
        self.assertEqual(theme_of('富国收益宝交易型货币A','货币型-普通货币'),'货币')
        self.assertEqual(theme_of('富国全球债券(QDII)人民币','QDII-混合债'),'海外/QDII')
        self.assertEqual(theme_of('富国某三年持有A','混合型'),'主动权益')

    def test_theme_watchlist_ranks_dedupes_and_flags(self):
        scores=pd.DataFrame({
            'mom20':[.10,.08,-.02,.05], 'mom60':[.20,.12,-.10,.15],
            'vol60':[.3,.25,.2,.4], 'drawdown60':[-.05,-.03,-.10,-.08],
        },index=['st1','st2','bd1','new1'])
        meta={
            'st1':{'name':'富国存储A','theme':'存储','group':'st1','otc':True,'history_rows':300,'model_supported':True,'buy_open':True},
            'st2':{'name':'富国存储C','theme':'存储','group':'st1','otc':True,'history_rows':300,'model_supported':True,'buy_open':True},
            'bd1':{'name':'富国纯债A','theme':'债券','group':'bd1','otc':True,'history_rows':300,'model_supported':True,'buy_open':True},
            'new1':{'name':'富国存储新A','theme':'存储','group':'new1','otc':True,'history_rows':150,'model_supported':True,'buy_open':False},
        }
        watch=theme_watchlist(scores,meta)
        self.assertTrue(watch['positive'])
        self.assertEqual(watch['themes'][0]['theme'],'存储')
        codes=[f['code'] for f in watch['funds']]
        self.assertIn('st1',codes)
        self.assertNotIn('st2',codes)
        new=next(f for f in watch['funds'] if f['code']=='new1')
        self.assertIn('短历史',new['flags'])
        self.assertIn('暂停申购',new['flags'])

    def test_analysis_codes_cover_model_unsupported_otc(self):
        cfg=load_config()
        snapshot={'funds':{
            '100001':{'name':'富国QDII测试','category':'QDII-普通股票','collection_status':'ok','analysis_ready':True,'research_ready':False,'history_rows':200},
            '100002':{'name':'富国测试货币A','category':'货币型-普通货币','collection_status':'model_unsupported','analysis_ready':False,'history_rows':None},
            '100003':{'name':'富国短历史','category':'混合型-偏股','collection_status':'insufficient_history','analysis_ready':False,'research_ready':False,'history_rows':100},
            '100004':{'name':'富国正常','category':'混合型-偏股','collection_status':'ok','analysis_ready':True,'research_ready':True,'history_rows':300},
            '100005':{'name':'富国场内ETF','category':'指数型-股票','collection_status':'not_otc','analysis_ready':False},
        }}
        codes=analysis_codes(cfg,snapshot)
        self.assertEqual(codes,['100001','100004'])
        self.assertNotIn('100002',codes)
        self.assertNotIn('100003',codes)
        self.assertNotIn('100005',codes)
        self.assertEqual(active_codes(cfg,snapshot),['100004'])


class StrategyUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.cfg=load_config()
        dates=pd.bdate_range('2021-01-01',periods=500)
        self.p=pd.DataFrame({'100001':np.exp(np.arange(500)*.001),'100002':np.exp(np.arange(500)*.0011),'100003':np.exp(np.arange(500)*.0005),'100004':np.exp(np.arange(500)*.0001)},index=dates)
        self.meta={c:{'group':('same' if c in ['100001','100002'] else c),'sector':('债券' if c=='100004' else c),'asset_type':('bond' if c=='100004' else 'equity')} for c in self.p}

    def test_representative_eligible_filters_group(self):
        codes=representative_codes(self.p,self.meta,128,eligible={'100002'})
        self.assertIn('100002',codes)
        self.assertNotIn('100001',codes)

    def test_drawdown_floor_excludes_deep_drawdown(self):
        p=self.p.copy()
        p.iloc[-60:,0]=np.linspace(p.iloc[-61,0],p.iloc[-61,0]*.7,60)
        w,_=target_weights(p,'momentum',self.cfg,self.meta)
        self.assertNotIn('100001',w)

    def test_breadth_haircut_reduces_sector_rotation_exposure(self):
        cfg=copy.deepcopy(self.cfg)
        cfg['signal']['breadth_haircut']={'risk_on':.5,'mixed':.5,'risk_off':.5,'defensive_only':.5,'unknown':.5}
        self.assertEqual(breadth_exposure(cfg,'risk_on'),.5)
        w,_=target_weights(self.p,'sector_rotation',cfg,self.meta)
        self.assertLessEqual(sum(w.values()),.475+1e-9)

    def test_new_fund_weight_cap(self):
        meta={c:dict(self.meta[c],history_rows=300) for c in self.meta}
        meta['100001']['history_rows']=130
        w,_=target_weights(self.p,'equal_weight',self.cfg,meta,eligible=set(meta),
                           min_history=128,max_weight_by_code={'100001':.10})
        self.assertLessEqual(w.get('100001',0),.10+1e-9)

    def test_equal_and_challengers_never_duplicate_share_group(self):
        for strategy in ('equal_weight',*CHALLENGERS):
            w,_=target_weights(self.p,strategy,self.cfg,self.meta)
            self.assertLessEqual(len(set(w)&{'100001','100002'}),1)
            self.assertLessEqual(sum(w.values()),.95+1e-9)
            self.assertTrue(all(v<=.35 for v in w.values()))

    def test_no_future_eligibility_for_shorter_share(self):
        p=self.p.copy();p.loc[p.index[:400],'100001']=np.nan
        candidates=representative_codes(p.iloc[:450],self.meta,128)
        self.assertNotIn('100001',candidates)
        self.assertIn('100002',candidates)

    def test_new_model_prefix_invariant(self):
        altered=self.p.copy();altered.iloc[350:]*=50
        for strategy in CHALLENGERS:
            a,_=target_weights(self.p.iloc[:300],strategy,self.cfg,self.meta)
            b,_=target_weights(altered.iloc[:300],strategy,self.cfg,self.meta)
            self.assertEqual(a,b)

    def test_quality_quarantine_uses_only_observed_breaks(self):
        p=self.p.copy()
        p.attrs['quality_breaks']={'100001':[p.index[300]]}
        self.assertIn('100001',representative_codes(p.iloc[:290],self.meta,128))
        self.assertNotIn('100001',representative_codes(p.iloc[:400],self.meta,128))
        self.assertIn('100001',representative_codes(p,self.meta,128))

    def test_optimized_eligibility_matches_prefix_reference(self):
        p=self.p.copy();p.iloc[280:290,2]=np.nan
        p.attrs['quality_breaks']=QualityEvents({'100001':[p.index[300]],'100003':[p.index[150]],'100004':[p.index[450]]})
        meta=copy.deepcopy(self.meta)
        for c in meta:meta[c]['required_history']=252
        for n in (270,350,430,500):
            prefix=p.iloc[:n];groups={}
            for c in sorted(prefix.columns):
                events=[x for x in p.attrs['quality_breaks'].get(c,[]) if x<=prefix.index[-1]]
                observed=prefix.loc[prefix.index>max(events),c] if events else prefix[c]
                if prefix[c].tail(128).notna().all() and observed.notna().sum()>=252:
                    groups.setdefault(meta[c]['group'],c)
            self.assertEqual(representative_codes(prefix,meta,128),list(groups.values()))

    def test_quality_metadata_cannot_mutate_through_dataframe_slice(self):
        events=QualityEvents({'100001':[self.p.index[300]]})
        p=self.p.copy();p.attrs['quality_breaks']=events
        self.assertIs(p.iloc[:200].attrs['quality_breaks'],events)
        with self.assertRaises(TypeError):events['100001']=()

    def test_held_quality_break_invalidates_accounting(self):
        cfg=copy.deepcopy(self.cfg)
        cfg['rules'].update(rebalance_sessions=1,max_fund_weight=1,max_sector_weight=1,max_exposure=1)
        p=self.p.copy();p.attrs['quality_breaks']={'100001':[p.index[155]]}
        p.iloc[155,0]=np.nan
        with patch('fullgoal_quant.backtest.target_weights',return_value=({'100001':1},pd.DataFrame())):
            with self.assertRaisesRegex(GuardError,'净值断点'):
                simulate(p,cfg,self.meta,'momentum',p.index[150],p.index[162])

    def test_rolling_windows_chronological_nonoverlap(self):
        result=folds(self.p.index,252,63)
        self.assertEqual(len(result),3)
        for (a,b),(c,d) in zip(result,result[1:]):
            self.assertLess(a,b);self.assertLess(b,c)

    def test_long_missing_held_nav_not_carried_forever(self):
        cfg=copy.deepcopy(self.cfg)
        cfg['rules'].update(rebalance_sessions=1,max_fund_weight=1,max_sector_weight=1,max_exposure=1)
        p=self.p.copy();p.iloc[153:160,0]=np.nan
        with patch('fullgoal_quant.backtest.target_weights',return_value=({'100001':1},pd.DataFrame())):
            with self.assertRaises(GuardError):
                simulate(p,cfg,self.meta,'momentum',p.index[150],p.index[162])

    def test_metrics_reject_missing_asset_days(self):
        with self.assertRaises(GuardError):metrics([200000,float('nan'),210000],200000)

    def test_unverifiable_research_is_not_replaced_by_zero_return(self):
        cfg=copy.deepcopy(self.cfg)
        cfg['research'].update(min_train_sessions=128,fold_sessions=63,holdout_sessions=63,min_folds=2)
        frames={c:pd.DataFrame({'tri':self.p[c]}) for c in self.p}
        def checked(prices,cfg,meta,strategy,start,end,status):
            if strategy!='cash':
                raise GuardError('fixture: unknowable held NAV')
            return simulate(prices,cfg,meta,strategy,start,end,status)
        with tempfile.TemporaryDirectory() as d,patch('fullgoal_quant.research.simulate',side_effect=checked):
            stale=Path(d)/'equal_weight-test-equity.csv';stale.write_text('obsolete result')
            r=evaluate(frames,cfg,self.meta,d)
            self.assertFalse(stale.exists())
        self.assertIsNone(r['results']['equal_weight']['test'])
        self.assertIn('test',r['results']['equal_weight']['errors'])
        self.assertEqual(r['results']['cash']['test']['total_return'],0)
        self.assertFalse(any(x['historical_gate_passed'] for x in r['challengers'].values()))

    def test_rules_verified_flags_cannot_inherit_fake_fees(self):
        cfg=copy.deepcopy(self.cfg)
        cfg['competition'].update(verified=True,rules_source='fixture',universe_source='fixture',allowed_codes=['100032'])
        cfg['fund_rules']={'100032':{'verified':True,'source':'fixture'}}
        self.assertTrue(any('不能继承' in e for e in competition_errors(cfg)))

    def test_account_must_be_numeric_and_targets_capped(self):
        with self.assertRaises(GuardError):
            validate_account({'as_of':'2026-10-19','cash':'200000','lots':[]})
        with self.assertRaises(GuardError):
            propose({'as_of':'2026-10-19','cash':200000,'lots':[]},{'100001':1},{'100001':1},{'100001':dict(buy_open=True,sell_open=True)},self.cfg,date(2026,10,19),['100001'],self.meta)


class DeliveryCalendarTests(unittest.TestCase):
    def test_reservation_survives_ambiguous_send_and_rerun(self):
        report={'report_id':'2026-10-19-afternoon','actionable':True}
        account={'cash':200000,'lots':[],'pending':[]}
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'state.json'
            self.assertTrue(reserve(report,account,path,owner='1'))
            self.assertFalse(reserve(report,account,path,owner='2'))
            later={**report,'report_id':'2026-10-20-afternoon'}
            with self.assertRaises(GuardError):reserve(later,account,path)
            account['acknowledged_plan_id']=report['report_id']
            self.assertTrue(reserve(later,account,path))
            complete(later['report_id'],path)
            self.assertEqual(state_at(path)['deliveries'][later['report_id']]['status'],'sent')

    def test_complete_remembers_last_sent_signal(self):
        report={'report_id':'2026-10-19-afternoon','actionable':True,
                'weights':{'100001':0.5},'assets':200000}
        account={'cash':200000,'lots':[],'pending':[]}
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'state.json'
            self.assertTrue(reserve(report,account,path,owner='1'))
            complete(report['report_id'],path,report=report)
            last=state_at(path)['last_signal']
            self.assertEqual(last['report_id'],report['report_id'])
            self.assertEqual(last['weights'],{'100001':0.5})
            self.assertEqual(last['assets'],200000)

    def test_unknown_year_never_filled_from_weekdays(self):
        with self.assertRaises(GuardError):parse_official_year('2026年休市安排',2027)
        with tempfile.TemporaryDirectory() as d,patch('fullgoal_quant.data.get_text',return_value='2026年休市安排'):
            with self.assertRaises(GuardError):extend_official(Calendar(),2027,Path(d)/'calendar.json')
            self.assertFalse((Path(d)/'calendar.json').exists())

    def test_official_parser_requires_all_sections(self):
        # Fixture exercises structure, never claims these are real 2027 closures.
        fixture='2027年休市安排'+''.join(x+'：1月1日至1月2日休市。' for x in ('元旦','春节','清明节','劳动节','端午节','中秋节','国庆节'))
        self.assertEqual(len(parse_official_year(fixture,2027)),7)
        with self.assertRaises(GuardError):parse_official_year(fixture.replace('国庆节','其他'),2027)


if __name__=='__main__':unittest.main()

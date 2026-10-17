"""Predeclared fixed-model rolling evaluation. No parameter search or automatic promotion."""
from __future__ import annotations

import hashlib
from pathlib import Path
import numpy as np
import pandas as pd
from .backtest import market_matrix, metrics, simulate
from .signals import STRATEGIES, BASE_STRATEGIES, CHALLENGERS, LOOKBACK
from .common import GuardError, fingerprint, now_cn, write_json


def model_hash():
    root = Path(__file__).parent
    return hashlib.sha256(b''.join((root/name).read_bytes() for name in ('signals.py','backtest.py','research.py','universe.py','data.py','common.py'))).hexdigest()


def folds(index, min_train=252, size=126):
    result=[]
    for pos in range(min_train,len(index),size):
        window=index[pos:pos+size]
        if len(window)==size:
            result.append((window[0],window[-1]))
    return result


def segment(curve, trades, start, end, initial):
    dates=pd.to_datetime(curve.date)
    mask=(dates>=start)&(dates<=end)
    before=curve[dates<start]
    capital=float(before.assets.iloc[-1]) if not before.empty else initial
    part=curve[mask]
    stat=metrics(part.assets.tolist(),capital)
    relevant=[t for t in trades if str(start.date())<=t['date']<=str(end.date())]
    stat.update(fees=float(sum(t['fee'] for t in relevant)),trades=len(relevant),
                turnover=float(sum(t['gross'] for t in relevant)/max(part.assets.mean(),1)))
    return stat


def evaluate(frames,cfg,metadata,out='reports/research',status_history=None):
    prices=market_matrix(frames)
    r=cfg['research']
    # Registered historical cutoff never moves automatically on a daily report run.
    cutoff=min(pd.Timestamp(r['evaluation_end']),pd.Timestamp(now_cn().date())-pd.Timedelta(days=1))
    prices=prices.loc['2021-01-01':cutoff]
    # Keep full preceding observations for feature warmup.
    all_prices=market_matrix(frames).loc[:cutoff]
    all_prices.attrs['research_cache_key']=hashlib.sha256(pd.util.hash_pandas_object(all_prices,index=True).values.tobytes()+fingerprint(metadata).encode()).hexdigest()
    ntest=r['holdout_sessions']
    eligible=prices.index
    if len(eligible)<r['min_train_sessions']+ntest+r['fold_sessions']*r['min_folds']:
        raise GuardError('不足以进行预注册的滚动验证；不缩短窗口来制造通过结果')
    dev,test=eligible[:-ntest],eligible[-ntest:]
    windows=folds(dev,r['min_train_sessions'],r['fold_sessions'])
    if len(windows)<r['min_folds']:
        raise GuardError('滚动样本外窗口不足')
    root=Path(out);root.mkdir(parents=True,exist_ok=True)
    manifest={'plan':r,'strategies':list(STRATEGIES),'model_hash':model_hash(),
              'config_hash':fingerprint(cfg),'cutoff':str(cutoff.date()),
              'folds':[[str(a.date()),str(b.date())] for a,b in windows],
              'holdout':[str(test[0].date()),str(test[-1].date())]}
    write_json(root/'manifest.json',manifest) # Save design before simulation results.
    results={};fold_stats={}
    for strategy in STRATEGIES:
        for suffix in ('-development.csv','-test-equity.csv','-test-trades.json'):
            (root/(strategy+suffix)).unlink(missing_ok=True)
        results[strategy]={'train':None,'validation':None,'test':None,'errors':{}}
        fold_stats[strategy]=[]
        try:
            _,curve,trades=simulate(all_prices,cfg,metadata,strategy,dev[0],dev[-1],status_history)
            results[strategy]['train']=segment(curve,trades,dev[0],dev[r['min_train_sessions']-1],cfg['competition']['initial_cash'])
            results[strategy]['validation']=segment(curve,trades,windows[0][0],windows[-1][1],cfg['competition']['initial_cash'])
            fold_stats[strategy]=[segment(curve,trades,a,b,cfg['competition']['initial_cash']) for a,b in windows]
            curve.to_csv(root/(strategy+'-development.csv'),index=False)
        except GuardError as exc:
            results[strategy]['errors']['development']=str(exc)
        try:
            stat,holdout,hold_trades=simulate(all_prices,cfg,metadata,strategy,test[0],test[-1],status_history)
            results[strategy]['test']=stat
            holdout.to_csv(root/(strategy+'-test-equity.csv'),index=False)
            write_json(root/(strategy+'-test-trades.json'),hold_trades)
        except GuardError as exc:
            results[strategy]['errors']['test']=str(exc)
    incumbent=r['incumbent']
    # Preserve incumbent: challengers are evidence reports, never overwrite it based on looked-at data.
    assessments={}
    for strategy in CHALLENGERS:
        if not fold_stats[strategy] or not fold_stats[incumbent] or results[strategy]['test'] is None or results[incumbent]['test'] is None:
            assessments[strategy]={'historical_gate_passed':False,'deployable':False,
                                  'reason':'策略或比较基准存在无法可靠计价的区段，完整滚动检验不成立'}
            continue
        excess=np.array([a['total_return']-b['total_return'] for a,b in zip(fold_stats[strategy],fold_stats[incumbent])])
        lower=float(excess.mean()-1.96*excess.std(ddof=1)/np.sqrt(len(excess)))
        val,hold=results[strategy]['validation'],results[strategy]['test']
        passed=bool(lower>0 and val['total_return']>0 and hold['total_return']>0
                    and hold['total_return']>results[incumbent]['test']['total_return']
                    and hold['max_drawdown']<=cfg['rules']['validation_max_drawdown'])
        assessments[strategy]={'historical_gate_passed':passed,'deployable':False,
             'fold_mean_excess':float(excess.mean()),'fold_excess_lower_approx_95':lower,
             'positive_excess_folds':int((excess>0).sum()),'fold_count':len(excess),
             'reason':'等待冻结方案后的真实前瞻记录；旧测试时段已查看，不能据此自动晋级'}
    if model_hash()!=manifest['model_hash']:
        raise GuardError('运行期间研究代码发生变化，本次结果作废，须重跑')
    result={'created_at':now_cn().isoformat(),'config_hash':fingerprint(cfg),'model_hash':model_hash(),
            'selected':incumbent,'challengers':assessments,'manifest':manifest,'fold_results':fold_stats,
            'selection_rule':'保留原等权模型；固定参数滚动检验新模型，不按已查看测试集改选赢家',
            'data_end':str(max(f.index[f.index.date<now_cn().date()][-1] for f in frames.values()).date()),
            'evaluation_end':str(cutoff.date()),'codes':sorted(frames),
            'historical_status':'point_in_time' if status_history is not None else 'assumed_open_not_competition_validated',
            'periods':{'train':[str(dev[0].date()),str(dev[r['min_train_sessions']-1].date())],
                       'validation':[str(windows[0][0].date()),str(windows[-1][1].date())],
                       'test':[str(test[0].date()),str(test[-1].date())]},'results':results,
            'deployment_approved':False,
            'limitations':['现有目录及已存续基金存在幸存者偏差，历史退市全池未取得',
                           '净值是供应商修订版；T-2仅缓解发布滞后，并非完整历史发布时间证明',
                           '历史费用/开放状态按配置假设，未核验比赛实际规则',
                           '滚动区间无重置现金或持仓；独立最终测试从相同现金开始，不能视作未查看样本',
                           '年化252；历史交易日以国内权益参考净值日期代理，未声称完整官方历史日历']}
    write_json(root/'comparison.json',result)
    lines=['# 固定方案滚动验证','',f'保留模型：{incumbent}；未自动推广任何新模型。','',
           '|策略|滚动样本外收益|最终历史测试收益|年化|最大回撤|夏普|双边换手/平均资产|费用|',
           '|---|---:|---:|---:|---:|---:|---:|---:|']
    for s,x in results.items():
        t=x['test'];v=x['validation']
        validation=f"{v['total_return']:.2%}" if v is not None else '无法验证'
        if t is None:
            lines.append(f"|{s}|{validation}|无法验证|—|—|—|—|—|")
        else:
            lines.append(f"|{s}|{validation}|{t['total_return']:.2%}|{t['annual_return']:.2%}|{t['max_drawdown']:.2%}|{t['sharpe']:.2f}|{t['turnover']:.2f}|{t['fees']:.2f}|")
    lines+=['','无法可靠计价的区段（未填造指标）：']
    lines += [f"- {s}: {x['errors']}" for s,x in results.items() if x['errors']]
    lines+=['','候选模型门槛：',str(assessments),'','局限：',*result['limitations']]
    (root/'comparison.md').write_text('\n'.join(lines),encoding='utf-8')
    return result

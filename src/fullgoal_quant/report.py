from __future__ import annotations

import html
from datetime import date, datetime, time
from pathlib import Path

import pandas as pd

from .backtest import market_matrix
from .calendar import Calendar
from .common import GuardError, TZ, competition_errors, competition_schedule, fingerprint, fund_rules, now_cn, read_json, write_json
from .portfolio import propose
from .signals import STRATEGIES, target_weights, score_universe
from .signals import market_regime
from .universe import metadata_for, active_codes, analysis_codes
from .research import model_hash


COVERAGE_COLUMNS = [
    'code', 'name', 'category', 'otc', 'model_supported', 'collection_status', 'collection_error',
    'analysis_ready', 'research_ready', 'history_rows', 'latest_nav_date', 'asset_type', 'sector',
    'group', 'share_class', 'buy_open', 'sell_open', 'purchase_limit', 'analysis_score', 'mom20',
    'mom60', 'vol60', 'trend', 'drawdown60', 'model_unsupported_reasons', 'data_notes',
    'selected_weight',
]


def _coverage(universe, snapshot, cfg, prices, weights):
    """Aggregate coverage plus one audit row per catalog fund.

    universe.json is the authoritative catalog (including funds that were not
    collected). When it is absent (direct test fixtures), fall back to the
    collected snapshot so existing callers keep working.
    """
    if universe and isinstance(universe.get('funds'), list):
        catalog = universe['funds']
        derived = False
    else:
        catalog = [{'code': code, **fund} for code, fund in snapshot['funds'].items()]
        derived = True
    scored = score_universe(prices) if not prices.empty else pd.DataFrame()
    scored_codes = set(str(x) for x in scored.index)

    def status_of(fund):
        return str(fund.get('collection_status') or ('ok' if derived else 'not_requested'))

    def analysis_ready(fund):
        if derived:
            return bool(fund.get('analysis_ready', status_of(fund) == 'ok'))
        return bool(fund.get('analysis_ready'))

    def research_ready(fund):
        if derived:
            return bool(fund.get('research_ready', analysis_ready(fund)))
        return bool(fund.get('research_ready'))

    summary = {
        'catalog': len(catalog),
        'otc': sum(1 for f in catalog if f.get('otc', True)),
        'not_otc': sum(1 for f in catalog if not f.get('otc', True)),
        'model_supported': sum(1 for f in catalog if f.get('otc', True) and f.get('model_supported', True)),
        'collected': sum(1 for f in catalog if f.get('history_rows') is not None),
        'analysis_ready': sum(1 for f in catalog if analysis_ready(f)),
        'scored': sum(1 for f in catalog if str(f.get('code', '')) in scored_codes),
        'full_backtest_ready': sum(1 for f in catalog if research_ready(f)),
        'new_fund_short_backtest': sum(1 for f in catalog if analysis_ready(f) and not research_ready(f)),
        'insufficient_history': sum(1 for f in catalog if status_of(f) == 'insufficient_history'),
        'model_unsupported': sum(1 for f in catalog if status_of(f) == 'model_unsupported'),
        'collection_errors': sum(1 for f in catalog if status_of(f).startswith('error')),
        'not_requested': sum(1 for f in catalog if status_of(f) == 'not_requested'),
    }
    rows = []
    for fund in catalog:
        code = str(fund.get('code', ''))
        score = scored.loc[code] if code in scored.index else None
        def value(key):
            return float(score[key]) if score is not None and pd.notna(score.get(key)) else None
        reasons = fund.get('model_unsupported_reasons') or []
        rows.append({
            'code': code,
            'name': fund.get('name', ''),
            'category': fund.get('category', ''),
            'otc': bool(fund.get('otc', True)),
            'model_supported': bool(fund.get('model_supported', True)),
            'collection_status': status_of(fund),
            'collection_error': fund.get('collection_error'),
            'analysis_ready': analysis_ready(fund),
            'research_ready': research_ready(fund),
            'history_rows': fund.get('history_rows'),
            'latest_nav_date': fund.get('latest_nav_date'),
            'asset_type': fund.get('asset_type', ''),
            'sector': fund.get('sector', ''),
            'group': fund.get('group', ''),
            'share_class': fund.get('share_class'),
            'buy_open': fund.get('buy_open'),
            'sell_open': fund.get('sell_open'),
            'purchase_limit': fund.get('purchase_limit'),
            'analysis_score': value('score'),
            'mom20': value('mom20'),
            'mom60': value('mom60'),
            'vol60': value('vol60'),
            'trend': value('trend'),
            'drawdown60': value('drawdown60'),
            'model_unsupported_reasons': ';'.join(str(x) for x in reasons),
            'data_notes': ';'.join(str(x) for x in (
                [fund['data_notes']] if isinstance(fund.get('data_notes'), str)
                else (fund.get('data_notes') or []))),
            'selected_weight': float(weights.get(code, 0.0)),
        })
    return summary, rows


def build_report(cfg, snapshot, frames, selection, account, *, research=False, at=None, slot="manual", calendar=None, delivery_state=None, universe=None):
    at = at or now_cn()
    if at.tzinfo is None:
        raise GuardError("报告时间须包含时区")
    at = at.astimezone(TZ)
    today = at.date()
    schedule = competition_schedule(cfg["competition"], today)
    cal = calendar or Calendar()
    # Fail on unknown calendar year, even in research mode.
    is_session = cal.is_session(today)
    # Research mode analyzes every OTC fund with a usable history; formal mode
    # still trusts only the verified competition whitelist.
    active = analysis_codes(cfg, snapshot) if research else active_codes(cfg, snapshot, competition=True)
    if not active:
        raise GuardError("比赛白名单为空；可使用 --research 生成明确标注的研究预览")
    metadata = metadata_for(cfg, snapshot)
    if set(active) - set(metadata):
        raise GuardError("请先给白名单中的所有基金配置 sector 和 group")
    if set(active) - set(frames) or set(active) - set(snapshot["funds"]):
        raise GuardError("本次快照未包含全部标的")
    errors = []
    chosen = selection['results'][selection['selected']]
    if chosen.get('validation') is None or chosen.get('test') is None:
        errors.append('所选模型存在无法可靠计价的历史区段，完整研究证据不足')
    if not research and (snapshot.get('errors') or not snapshot.get('universe_complete',False)):
        errors.append('本次正式数据集不完整或仅为抽样验收')
    if selection["config_hash"] != fingerprint(cfg) or sorted(selection["codes"]) != sorted(active):
        errors.append("策略选择文件与当前配置/基金池不一致，请重新回测")
    selected_end = date.fromisoformat(selection["data_end"])
    if selected_end >= today or (today - selected_end).days > cfg["model"]["max_selection_age_days"]:
        errors.append("策略选择文件过期或包含当前日/未来净值")
    if not research:
        errors += competition_errors(cfg)
        if not cfg["rules"].get("verified"):
            errors.append("风险与交易规则尚未核验")
        if selection.get('model_hash') != model_hash() or not selection.get('deployment_approved',False):
            errors.append('当前模型尚无经确认的部署证据，不能仅凭历史排名启用')
        stamp=account.get('reconciled_at')
        if account.get('as_of')!=str(today) or not stamp:
            errors.append('正式决策需要当日账户核对日期及带时区的 reconciled_at')
        elif not 0 <= (at-datetime.fromisoformat(stamp).astimezone(TZ)).total_seconds() <= 4*3600:
            errors.append('实际账户超过4小时未核对')
        if delivery_state is None:
            errors.append('持久化通知/调仓核对状态不可用')
        else:
            from .notifications import account_hash
            last=delivery_state.get('last_actionable')
            if last and last['account_hash']==account_hash(account) and account.get('acknowledged_plan_id')!=last['id']:
                errors.append('上一份可执行计划尚未核对，不重复建议下单')
        oos = selection["results"][selection["selected"]]["test"]
        if oos is None or oos["total_return"] <= 0 or oos["max_drawdown"] > cfg["rules"]["validation_max_drawdown"]:
            errors.append("所选模型独立测试未通过部署风险门槛，保留现金并继续研究；不按测试集改选赢家")
        for code in active:
            rules = cfg.get("fund_rules", {}).get(code, {})
            if not rules.get("verified") or not rules.get("source"):
                errors.append(f"{code} 的比赛费率/申赎规则尚未逐只核验")
        if today < date.fromisoformat(cfg["competition"]["start_date"]):
            errors.append("比赛尚未开始")
        if cfg["competition"].get("end_date") and today > date.fromisoformat(cfg["competition"]["end_date"]):
            errors.append("比赛已经结束")
    signal_strategy = cfg.get('signal', {}).get('strategy', selection['selected'])
    if signal_strategy not in STRATEGIES:
        raise GuardError(f"signal.strategy 未知：{signal_strategy}")
    if not research and signal_strategy != selection['selected']:
        errors.append('信号策略与已批准的基准模型不一致；不生成可执行金额')
    signal_stats = selection.get('results', {}).get(signal_strategy)
    previous = cal.previous(today)
    safe_frames, navs, nav_dates = {}, {}, {}
    for code in active:
        frame = frames[code][frames[code].index.date < today]
        if frame.empty:
            raise GuardError(f"{code} 没有已完成日期净值")
        latest = frame.index[-1].date()
        nav_dates[code] = str(latest)
        navs[code] = float(frame.unit_nav.iloc[-1])
        safe_frames[code] = frame
        age = (at - datetime.fromisoformat(snapshot["funds"][code]["fetched_at"]).astimezone(TZ)).total_seconds() / 3600
        if age < 0 or age > cfg["data"]["max_snapshot_age_hours"]:
            errors.append(f"{code} 申赎状态快照过期，请重新采集")
        if cal.lag(latest, previous) > cfg["data"]["max_nav_lag_sessions"]:
            errors.append(f"{code} 净值滞后过多")
    prices = market_matrix(safe_frames)
    # Match backtest decision lag: execution session T uses NAV through T-2.
    cutoff = cal.previous(today, cfg["rules"]["signal_lag_sessions"])
    prices = prices.loc[:str(cutoff)]
    # Only currently tradeable, model-supported funds may receive weights.
    # The analysis/coverage universe is deliberately wider.
    buyable = set()
    for code in active:
        fund = snapshot['funds'][code]
        if not fund.get('model_supported', True):
            continue
        if not fund.get('buy_open', True):
            continue
        limit = fund.get('purchase_limit')
        if limit is not None and limit < cfg['rules']['min_order']:
            continue
        buyable.add(code)
    min_score_history = cfg.get('universe', {}).get('min_score_history', 128)
    full_history = cfg.get('universe', {}).get('min_history', 252)
    new_fund_cap = cfg.get('signal', {}).get('new_fund_max_weight', 0.10)
    max_weight_by_code = {
        c: new_fund_cap for c in active
        if snapshot['funds'][c].get('history_rows') is not None
        and snapshot['funds'][c]['history_rows'] < full_history
    }
    weights, scores = target_weights(
        prices, signal_strategy, cfg, metadata, eligible=buyable,
        min_history=min_score_history, max_weight_by_code=max_weight_by_code)
    coverage, coverage_rows = _coverage(universe, snapshot, cfg, prices, weights)
    if scores.empty:
        errors.append('当前历史长度或连续性不足，无法生成可靠调仓建议')
    regime, breadth = market_regime(scores, metadata)
    target_day = today
    end = date.fromisoformat(schedule["end_date"]) if schedule["end_date"] else None
    if end and (today > end or (today == end and at.hour >= 15)):
        target_day = None
    elif not is_session or at.hour >= 15:
        target_day = cal.next(today)
    if end and target_day and target_day > end:
        target_day = None
    if target_day is None:
        errors.append("比赛截止后无可提交交易日，仅保留账户与结果复核")
    # Estimate signals against the plan date, not the wall-clock date: an
    # evening/weekend report may describe the next session's weekly rebalance.
    rebalance_due = False
    if target_day is not None:
        anchor = date.fromisoformat(cfg["competition"]["start_date"])
        before_target = previous if target_day == today else cal.previous(target_day)
        if cfg['rules'].get('rebalance_mode')=='weekly' and cfg['rules']['rebalance_sessions']==5:
            rebalance_due = target_day >= anchor and target_day.isocalendar()[:2] != before_target.isocalendar()[:2]
        else:
            rebalance_due = target_day >= anchor and cal.lag(anchor, target_day) % cfg["rules"]["rebalance_sessions"] == 0
    proposal = propose(account, weights, navs, snapshot["funds"], cfg, today, active, metadata)
    errors += proposal["errors"]
    # Research gets hypothetical amounts, always labeled. Formal failures get no orders.
    in_window = slot=='afternoon' and time(14,20) <= at.time().replace(tzinfo=None) <= time(14,55)
    actionable = (not research and not errors and rebalance_due and in_window
                  and is_session and target_day == today)
    orders = proposal["orders"] if actionable else []
    # By default, a sent research signal is treated as executed for the next
    # run, unless a reconciled real account is supplied. This lets the next
    # signal produce add/reduce/clear directions without manual account edits.
    previous_signal = (delivery_state or {}).get('last_signal')
    actual_positions = bool(account.get('lots')) or bool(account.get('pending')) or bool(account.get('reconciled_at'))
    previous_weights = {}
    if (research and cfg.get('signal', {}).get('assume_execution')
            and previous_signal and not actual_positions):
        previous_weights = {str(c): float(v)
                            for c, v in (previous_signal.get('weights') or {}).items()
                            if isinstance(v, (int, float)) and v > 0}
    # Signal rows are target weights/directions, never amounts. They are built
    # from the account's lots when available; an empty account shows建仓.
    def signal_row_list(account, weights, navs, assets, snapshot_funds, previous_weights=None):
        current = {}
        if previous_weights is None:
            for lot in account.get('lots', []):
                code = lot.get('code')
                if code in navs:
                    current[code] = current.get(code, 0.0) + float(lot.get('units', 0)) * navs[code]
            current_weights = {c: (v / assets if assets > 0 else 0.0) for c, v in current.items()}
        else:
            current_weights = previous_weights
        rows = []
        codes = sorted(set(weights) | {c for c, v in current_weights.items() if v > 0},
                       key=lambda c: (-weights.get(c, 0.0), c))
        for code in codes:
            target = float(weights.get(code, 0.0))
            current_weight = current_weights.get(code, 0.0)
            if target <= 0 and current_weight <= 0:
                continue
            if target <= 1e-9:
                action = '清仓/卖出'
            elif current_weight <= 1e-9:
                action = '买入/建仓'
            elif target - current_weight > 0.02:
                action = '加仓'
            elif target - current_weight < -0.02:
                action = '减仓'
            else:
                action = '持有'
            if action in ('减仓', '清仓/卖出'):
                reference = max(0.0, current_weight - target) * max(assets, 0.0)
            else:
                reference = target * max(assets, 0.0)
            rows.append({'code': code,
                         'name': snapshot_funds.get(code, {}).get('name', code),
                         'sector': snapshot_funds.get(code, {}).get('sector', ''),
                         'target_weight': round(target, 6),
                         'current_weight': round(current_weight, 6),
                         'reference_amount': round(reference, 2),
                         'action': action})
        return rows[:12]

    signal_rows = [] if scores.empty else signal_row_list(
        account, weights, navs, proposal['assets'], snapshot['funds'],
        previous_weights=previous_weights or None)
    total_weight = float(sum(weights.values()))
    cash_weight = max(0.0, 1.0 - total_weight)
    signal_lines = [f"{r['code']} {r['name']}｜目标 {r['target_weight']:.1%}｜{r['action']}"
                    f"｜{'卖出约' if r['action'] in ('减仓', '清仓/卖出') else '参考'} ¥{r['reference_amount']:,.0f}"
                    for r in signal_rows] or ["当前无有效信号，维持现金/现有持仓并核对数据。"]

    def fmt_stat(stat):
        if not stat or stat.get('total_return') is None:
            return "无法验证"
        result = f"{stat['total_return']:.2%}"
        if stat.get('max_drawdown') is not None:
            result += f"（回撤 {stat['max_drawdown']:.2%}）"
        return result

    def esc(value):
        return html.escape(str(value), quote=False)

    action_icon = {'买入/建仓': '🟢', '加仓': '🟢', '持有': '⚪',
                   '减仓': '🟠', '清仓/卖出': '🔴'}
    action_priority = {'买入/建仓': 0, '加仓': 1, '持有': 2,
                       '减仓': 3, '清仓/卖出': 4}
    ordered = sorted(signal_rows,
                     key=lambda r: (action_priority.get(r['action'], 9),
                                    -r['target_weight'], r['code']))
    operation_lines = []
    for index, row in enumerate(ordered, 1):
        sector = row.get('sector') or '未分类'
        amount_label = '卖出约' if row['action'] in ('减仓', '清仓/卖出') else '参考'
        operation_lines.append(
            f"{index}. <b>{esc(row['code'])} {esc(row['name'])}</b>（{esc(sector)}）\n"
            f"   {action_icon.get(row['action'], '⚪')} {esc(row['action'])}"
            f"｜目标 <b>{row['target_weight']:.1%}</b>"
            f"｜{amount_label} <b>¥{row['reference_amount']:,.0f}</b>")
    if not operation_lines:
        operation_lines = ["⚪ 无有效买入/调仓信号，维持现金或现有持仓。"]

    deadline = cfg.get('signal', {}).get('buy_deadline', '15:00')
    if cfg.get('signal', {}).get('daily'):
        deadline_line = f"• 截止：{deadline} 前按目标权重买入/调整"
    else:
        deadline_line = "• 周度首个交易日按目标权重调仓"
    execution_lines = [
        f"• 计划交易日：{target_day or '无（已截止）'}",
        deadline_line,
        f"• 目标组合：合计 {total_weight:.1%}，现金 {cash_weight:.1%}",
        "• 参考金额按账户估值估算；限购/暂停申购以平台页面为准",
    ]
    if previous_weights:
        execution_lines.append("• 按上一份已发送信号视为已执行；实际成交不同请以持仓为准")
    execution_lines.append("• 执行条件已具备，仍须本人核对规则与持仓"
                           if actionable else
                           "• 研究信号，非成交指令；不提供可执行金额")
    risk_lines = []
    if signal_stats:
        risk_lines.append(f"• 策略历史：滚动验证 {fmt_stat(signal_stats.get('validation'))}"
                          f"；最终测试 {fmt_stat(signal_stats.get('test'))}")
    risk_lines.append("• 历史区间已查看，不冒充前瞻；不保证收益")
    if errors:
        risk_lines.append(f"• 注意：{len(errors)}项数据/规则/账户阻断，详情见报告")
    has_buy = any(row['action'] in ('买入/建仓', '加仓') for row in signal_rows)
    telegram_text = "\n".join([
        f"{'🟢' if has_buy else '⚪'} <b>{esc(cfg['competition']['name'])}操作信号</b>",
        f"{at:%Y-%m-%d %H:%M}｜策略 <b>{esc(signal_strategy)}</b>｜市场 {esc(regime)}",
        "━━━━━━━━━━━━━━",
        "<b>① 买什么 / 调仓</b>",
        *operation_lines,
        "",
        "<b>② 为什么这么配</b>",
        f"• 市场：{esc(regime)}，正趋势广度 {breadth:.0%}",
        "• 选基：行业 20 日动量前 2；行业内 60 日动量为正、综合评分前 3",
        "• 约束：单基金≤35% / 单行业≤50% / 总仓位≤95%",
        f"• 覆盖：场外{coverage['otc']}只全量扫描；可评分{coverage['analysis_ready']}只"
        f"（完成评分{coverage['scored']}只，短历史{coverage['new_fund_short_backtest']}只）；"
        f"模型不支持{coverage['model_unsupported']}只；采集异常{coverage['collection_errors']}只",
        "",
        "<b>③ 怎么执行</b>",
        *execution_lines,
        "",
        "<b>④ 风险</b>",
        *risk_lines,
    ])
    if research:
        status = "研究信号（非比赛交易指令）"
    elif errors:
        status = "暂缓操作"
    elif actionable:
        status = "比赛账户建议"
    else:
        status = "观察（未到执行窗口或非调仓时点）"
    def countdown(days):
        return "待确认" if days is None else (f"剩余{days}自然日" if days >= 0 else f"已过{-days}自然日")
    lines = [f"{cfg['competition']['name']}｜{status}", f"北京时间 {at:%Y-%m-%d %H:%M}｜时段 {slot}",
             f"赛程：{schedule['start_date']} 开始｜{schedule['midpoint_date'] or '待确认'} 半程截止｜{schedule['end_date'] or '待确认'} 结束",
             f"当前阶段：{schedule['phase']}｜半程{countdown(schedule['days_to_midpoint'])}｜全程{countdown(schedule['days_to_end'])}",
             f"计划交易日 {target_day or '无（比赛已截止）'}｜信号策略 {signal_strategy}｜基准模型 {selection['selected']}｜信号截止 {cutoff}",
             f"账户估值 ¥{proposal['assets']:,.2f}｜可用现金 ¥{account['cash']:,.2f}",
             "净值为已完成日期数据；不把盘中估值或尚未公布的当日净值当成成交价。"]
    if end and end > cal.end:
        lines.append(f"交易日历仅核验至 {cal.end}；须在跨年前按交易所公告补齐2027年安排，不按普通工作日猜测。")
    lines.append(f"市场状态（基金净值广度代理，非盘中预测）：{regime}，正趋势广度 {breadth:.0%}")
    lines.append(f"执行条件：{'已具备（仍须本人核对）' if actionable else '未具备，仅作观察，不提供操作金额'}")
    lines.append("今日信号（目标权重，非金额）：")
    lines += ["  " + x for x in signal_lines]
    lines.append(f"  现金 {cash_weight:.1%}")
    if previous_weights:
        lines.append("上一份信号按已执行处理；若实际成交不同，请设置 ACCOUNT_JSON 以实际持仓覆盖。")
    lines.append(f"数据覆盖：目录{coverage['catalog']}只；场外{coverage['otc']}只全量扫描"
                 f"（可评分{coverage['analysis_ready']}只、完成评分{coverage['scored']}只，"
                 f"其中完整回测{coverage['full_backtest_ready']}只、"
                 f"短历史{coverage['new_fund_short_backtest']}只；历史不足{coverage['insufficient_history']}只；"
                 f"模型不支持{coverage['model_unsupported']}只；采集异常{coverage['collection_errors']}只；"
                 f"非场外{coverage['not_otc']}只）；完整清单见 reports/universe_coverage.csv。")
    if not is_session or at.hour >= 15:
        lines.append("当前不可按当日净值提交；以下仅供下一交易日前复核。")
    if not rebalance_due:
        if cfg.get('signal', {}).get('daily'):
            lines.append("信号每日生成；建议在当日结算前按目标权重买入/调整；历史回测仍采用周度首个交易日规则。")
        else:
            lines.append("当前不是模型约定的调仓时点；实盘与回测采用相同周度首个交易日规则。")
    if research:
        lines.append("研究池不是比赛白名单，费用/到账天数为配置假设；研究报告不生成可执行金额。")
        if chosen.get('test') is None or chosen['test']["total_return"] <= 0:
            lines.append("所选模型独立测试亏损，尚不具备部署依据；以下仅展示计算流程。")
    if errors:
        lines += ["暂停生成操作金额：", *["• " + x for x in errors]]
    lines.append("基金评分与目标仓位：")
    display=list(scores.sort_values('score',ascending=False).head(12).index) if not scores.empty else []
    display=list(dict.fromkeys(display+[c for c,w in sorted(weights.items(),key=lambda x:-x[1])[:12]]+[x['code'] for x in account['lots'] if x['code'] in scores.index]))
    if len(weights)>12:
        lines.append(f'目标组合共{len(weights)}只份额；此处显示摘要，完整分数与目标保存在JSON，本基准未获准实盘部署。')
    for code, row in scores.reindex(display).iterrows():
        weight = weights.get(code, 0)
        lines.append(f"{code} {snapshot['funds'][code]['name']}｜评分 {row['score']:.1f}/100｜目标 {weight:.1%}｜净值日 {nav_dates[code]}")
        lines.append(f"  20日 {row['mom20']:+.2%} / 60日 {row['mom60']:+.2%}；60日年化波动 {row['vol60']:.2%}；120日均线偏离 {row['trend']:+.2%}")
        if not actionable:
            holding = any(x['code']==code for x in account['lots'])
            lines.append('  '+('继续持有并观察，未给出调仓金额' if holding else '观察标的，未给出买入金额'))
    if scores.empty:
        lines.append("有效样本不足，维持现金。")
    lines.append("持仓调整（单份目标，请勿将三个时段的金额相加）：")
    for order in orders:
        units = f"，赎回 {order['units']:.2f} 份" if order["units"] else ""
        action = order["action"]
        if not research and not rebalance_due:
            action = "观察目标差额，暂不提交"
        lines.append(f"{order['code']} {action} ¥{order['amount']:,.2f}{units}｜目标 {order['target_weight']:.1%}｜估计费用 ¥{order['fee_estimate']:.2f}")
        lines.append("  " + order["reason"])
    if not orders:
        lines.append("暂无可提交调仓建议；维持当前已确认持仓与现金并核对限制。")
    validation = selection["results"][selection["selected"]]["validation"]
    test = selection["results"][selection["selected"]]["test"]
    lines.append(f"依据：{selection.get('selection_rule','保留基准模型，测试结果不作为改选赢家依据')}。")
    if validation is None or test is None:
        lines.append(f"历史研究有无法可靠计价区段，不展示拼接收益：{chosen.get('errors',{})}")
    else:
        lines += [f"开发验证收益 {validation['total_return']:.2%}。",
                  f"最终历史测试：收益 {test['total_return']:.2%}、最大回撤 {test['max_drawdown']:.2%}、夏普 {test['sharpe']:.2f}；该区间已查看，不冒充前瞻业绩。"]
    if signal_stats is not None:
        lines.append(f"信号策略 {signal_strategy} 历史：滚动验证 {fmt_stat(signal_stats.get('validation'))}；"
                     f"最终测试 {fmt_stat(signal_stats.get('test'))}；该区间已查看，不冒充前瞻。")
    lines += [
              "风险：趋势反转、行业集中、净值滞后、申购限额、短期赎回费与赎回到账延迟；未知价交易的实际金额/仓位会偏离估算。",
              "历史申赎状态：" + selection["historical_status"] + "；当前状态不能倒填过去。",
              "复权净值回测假设红利再投，历史样本存在存续偏差及供应商事后修订；回测不保证收益或比赛排名。",
              "本系统只生成建议，由本人在比赛平台核对规则和实际持仓后操作。"]
    result = {"created_at": at.isoformat(), "mode": "research" if research else "competition", "status": status,
              "strategy": signal_strategy, "signal_strategy": signal_strategy,
              "selected_model": selection["selected"], "signal": signal_rows,
              "assumed_execution": bool(previous_weights),
              "previous_signal_id": previous_signal.get('report_id') if previous_signal else None,
              "assets": proposal['assets'], "telegram_text": telegram_text,
              "target_day": str(target_day) if target_day else None, "signal_cutoff": str(cutoff),
              "schedule": schedule,
              "rebalance_due": rebalance_due, "weights": weights, "orders": orders, "errors": errors,
              "actionable": actionable, "slot":slot, "market_regime":regime,
              "score_rows":scores.reset_index(names='code').to_dict('records'),
              "coverage": coverage, "coverage_rows": coverage_rows,
              "universe_complete": bool(snapshot.get('universe_complete', False)),
              "report_id": f"{today}-{slot}", "text": "\n".join(lines)}
    return result


def save_report(report, out="reports"):
    target = Path(out)
    target.mkdir(parents=True, exist_ok=True)
    write_json(target / "latest.json", report)
    (target / "latest.txt").write_text(report["text"], encoding="utf-8")
    (target / (report["report_id"] + ".txt")).write_text(report["text"], encoding="utf-8")
    if report.get("coverage_rows"):
        pd.DataFrame(report["coverage_rows"]).reindex(columns=COVERAGE_COLUMNS).to_csv(
            target / "universe_coverage.csv", index=False)

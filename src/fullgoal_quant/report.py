from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

import pandas as pd

from .backtest import market_matrix
from .calendar import Calendar
from .common import GuardError, TZ, competition_errors, competition_schedule, fingerprint, fund_rules, now_cn, read_json, write_json
from .portfolio import propose
from .signals import STRATEGIES, target_weights
from .signals import market_regime
from .universe import metadata_for, active_codes
from .research import model_hash


def build_report(cfg, snapshot, frames, selection, account, *, research=False, at=None, slot="manual", calendar=None, delivery_state=None):
    at = at or now_cn()
    if at.tzinfo is None:
        raise GuardError("报告时间须包含时区")
    at = at.astimezone(TZ)
    today = at.date()
    schedule = competition_schedule(cfg["competition"], today)
    cal = calendar or Calendar()
    # Fail on unknown calendar year, even in research mode.
    is_session = cal.is_session(today)
    active = active_codes(cfg, snapshot, competition=not research)
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
    weights, scores = target_weights(prices, signal_strategy, cfg, metadata)
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
    # Signal rows are target weights/directions, never amounts. They are built
    # from the account's lots when available; an empty account shows建仓.
    def signal_row_list(account, weights, navs, assets, snapshot_funds):
        current = {}
        for lot in account.get('lots', []):
            code = lot.get('code')
            if code in navs:
                current[code] = current.get(code, 0.0) + float(lot.get('units', 0)) * navs[code]
        rows = []
        codes = sorted(set(weights) | {c for c, v in current.items() if v > 0},
                       key=lambda c: (-weights.get(c, 0.0), c))
        for code in codes:
            target = float(weights.get(code, 0.0))
            current_weight = current.get(code, 0.0) / assets if assets > 0 else 0.0
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
            rows.append({'code': code,
                         'name': snapshot_funds.get(code, {}).get('name', code),
                         'target_weight': round(target, 6),
                         'current_weight': round(current_weight, 6),
                         'action': action})
        return rows[:12]

    signal_rows = [] if scores.empty else signal_row_list(
        account, weights, navs, proposal['assets'], snapshot['funds'])
    total_weight = float(sum(weights.values()))
    cash_weight = max(0.0, 1.0 - total_weight)
    signal_lines = [f"{r['code']} {r['name']}｜目标 {r['target_weight']:.1%}｜{r['action']}"
                    for r in signal_rows] or ["当前无有效信号，维持现金/现有持仓并核对数据。"]

    def fmt_stat(stat):
        if not stat or stat.get('total_return') is None:
            return "无法验证"
        result = f"{stat['total_return']:.2%}"
        if stat.get('max_drawdown') is not None:
            result += f"（回撤 {stat['max_drawdown']:.2%}）"
        return result

    telegram_lines = [
        f"{cfg['competition']['name']}信号｜{at:%Y-%m-%d %H:%M}｜策略 {signal_strategy}｜市场 {regime}",
        f"目标组合（合计 {total_weight:.1%}，现金 {cash_weight:.1%}）：",
        *signal_lines,
        f"信号截止 {cutoff}；计划交易日 {target_day or '无（已截止）'}。",
        "执行条件已具备；仍须本人核对规则与持仓后操作。" if actionable
        else "研究信号，非成交指令；不提供可执行金额。",
    ]
    if signal_stats:
        telegram_lines.append(
            f"信号策略历史：滚动验证 {fmt_stat(signal_stats.get('validation'))}；"
            f"最终测试 {fmt_stat(signal_stats.get('test'))}（已查看，不冒充前瞻）。")
    telegram_lines.append("周度首个交易日调仓；不保证收益。")
    if errors:
        telegram_lines.append(f"注意：{len(errors)}项数据/规则/账户阻断，详情见报告。")
    telegram_text = "\n".join(telegram_lines)
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
    lines.append(f"数据覆盖：本次成功{len(snapshot['funds'])}只；采集异常{len(snapshot.get('errors',{}))}只；完整目录见universe.json。")
    if not is_session or at.hour >= 15:
        lines.append("当前不可按当日净值提交；以下仅供下一交易日前复核。")
    if not rebalance_due:
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
              "telegram_text": telegram_text,
              "target_day": str(target_day) if target_day else None, "signal_cutoff": str(cutoff),
              "schedule": schedule,
              "rebalance_due": rebalance_due, "weights": weights, "orders": orders, "errors": errors,
              "actionable": actionable, "slot":slot, "market_regime":regime,
              "score_rows":scores.reset_index(names='code').to_dict('records'),
              "report_id": f"{today}-{slot}", "text": "\n".join(lines)}
    return result


def save_report(report, out="reports"):
    target = Path(out)
    target.mkdir(parents=True, exist_ok=True)
    write_json(target / "latest.json", report)
    (target / "latest.txt").write_text(report["text"], encoding="utf-8")
    (target / (report["report_id"] + ".txt")).write_text(report["text"], encoding="utf-8")

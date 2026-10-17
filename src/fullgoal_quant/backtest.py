from __future__ import annotations

from dataclasses import asdict, dataclass
from collections.abc import Mapping
from types import MappingProxyType
from pathlib import Path

import numpy as np
import pandas as pd

from .common import GuardError, fingerprint, fund_rules, now_cn, redemption_fee, write_json
from .signals import LOOKBACK, STRATEGIES, BASE_STRATEGIES, target_weights, market_regime


@dataclass
class Lot:
    code: str
    units: float
    confirmed: pd.Timestamp


class QualityEvents(Mapping):
    """Immutable event tuples, safe to share across pandas' metadata copies."""
    def __init__(self, events):
        self._events = MappingProxyType({c:tuple(values) for c,values in events.items() if len(values)})

    def __getitem__(self, key):
        return self._events[key]

    def __iter__(self):
        return iter(self._events)

    def __len__(self):
        return len(self._events)

    def __deepcopy__(self, memo):
        return self


def market_matrix(frames):
    # Union preserves real missing NAVs. No bfill; stale valuations may be carried
    # for accounting only. Signals and fills explicitly require observed values.
    p = pd.concat({c: f.tri for c, f in frames.items()}, axis=1).sort_index()
    # A domestic equity reference NAV calendar prevents weekend/holiday bond NAVs
    # from accelerating settlement. Historical reference is an explicitly disclosed proxy.
    if '100038' in frames:
        p = p.reindex(frames['100038'].index)
    p = p[p.index.dayofweek < 5]
    p.attrs['quality_breaks'] = QualityEvents({c: f.index[f.get('quality_break', pd.Series(False,index=f.index))]
                                             for c,f in frames.items()})
    return p


def metrics(equity, initial, rf=0):
    series = pd.Series([initial] + list(equity), dtype=float)
    if not np.isfinite(series).all() or (series<=0).any():
        raise GuardError('资产序列非有限正数，不能跳过异常日计算指标')
    returns = series.pct_change().dropna()
    n = len(returns)
    std = returns.std(ddof=1)
    return {"total_return": float(series.iloc[-1] / initial - 1),
            "annual_return": float((series.iloc[-1] / initial) ** (252 / max(n, 1)) - 1),
            "max_drawdown": float(-(series / series.cummax() - 1).min()),
            "sharpe": float((returns.mean() - rf / 252) / std * np.sqrt(252)) if std > 1e-12 else 0.0,
            "final_assets": float(series.iloc[-1]), "sessions": n}


def simulate(prices, cfg, metadata, strategy, start, end, status_history=None):
    """Orders decided before execution NAV; two-session signal lag by default.

    Adjusted total-return units assume dividends reinvested. Cash has zero return.
    Fees, confirmation, FIFO lockup and delayed redemption cash are explicit.
    Historical status is optional and must be point-in-time, not today's status.
    """
    dates = prices.index
    cash = float(cfg["competition"]["initial_cash"])
    initial = cash
    lots, pending, trades, records = [], [], [], []
    regime = 'unknown'
    marks = prices.ffill(limit=cfg['data']['max_nav_lag_sessions'])
    marks.attrs = {}
    lag = cfg["rules"]["signal_lag_sessions"]
    rebalance = cfg["rules"]["rebalance_sessions"]
    sessions = [int(x) for x in np.flatnonzero((dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end)))]
    if not sessions:
        raise GuardError("回测区间无净值记录")
    break_sessions = {}
    for code, events in prices.attrs.get('quality_breaks', {}).items():
        for event in events:
            pos = int(dates.searchsorted(event))
            break_sessions.setdefault(pos, set()).add(code)

    def nav_value(i):
        if any(pd.isna(marks.iloc[i][x.code]) for x in lots):
            raise GuardError(f'{dates[i].date()} 存量持仓净值长期缺失；该回测不能可靠计价')
        return cash + sum(x.units * marks.iloc[i][x.code] for x in lots) + sum(x[1] for x in pending)

    for step, i in enumerate(sessions):
        day = dates[i]
        if any(x.code in break_sessions.get(i, set()) for x in lots):
            raise GuardError(f'{day.date()} 持仓跨越无法解释净值断点；该策略回测无效，禁止拼接收益')
        due = [x for x in pending if x[0] <= i]
        cash += sum(x[1] for x in due)
        pending = [x for x in pending if x[0] > i]
        due = step % rebalance == 0
        if cfg['rules'].get('rebalance_mode') == 'weekly' and rebalance == 5:
            due = i == 0 or dates[i].isocalendar()[:2] != dates[i-1].isocalendar()[:2]
        if due and i - lag >= LOOKBACK - 1:
            signal_day = dates[i - lag]
            weights, f = target_weights(prices.iloc[:i - lag + 1], strategy, cfg, metadata)
            regime, _ = market_regime(f, metadata)
            known = marks.iloc[i - lag]
            if any(pd.isna(known[x.code]) for x in lots):
                raise GuardError('已知信息时点的持仓估值过期，停止回测')
            total = cash + sum(x.units * known[x.code] for x in lots) + sum(x[1] for x in pending)
            held = {c: sum(x.units for x in lots if x.code == c) * known[c] for c in prices.columns if pd.notna(known[c])}

            def allowed(code, side):
                if pd.isna(prices.iloc[i][code]):
                    return False
                if status_history is None:
                    return True
                eligible = status_history[(status_history.code == code) & (status_history.date <= day)]
                if eligible.empty:
                    return False
                return bool(eligible.sort_values("date").iloc[-1][side + "_open"])

            # Redemptions first, but proceeds cannot fund buys before settlement.
            for code in prices.columns:
                rules = fund_rules(cfg, code)
                if code not in held:
                    continue
                desired_sale = held[code] - total * weights.get(code, 0)
                if desired_sale < rules["min_order"] or not allowed(code, "sell"):
                    continue
                price = float(prices.iloc[i][code])
                remaining = desired_sale / float(known[code])
                for lot in [x for x in lots if x.code == code]:
                    age = (day - lot.confirmed).days
                    if day < lot.confirmed or age < rules["min_hold_days"]:
                        continue
                    units = min(lot.units, remaining)
                    if units <= 1e-10:
                        break
                    fee_rate = redemption_fee(rules, age) + rules["slippage_bps"] / 10000
                    gross = units * price
                    proceeds = gross * (1 - fee_rate)
                    cash_index = i + rules["redemption_sessions"]
                    if cash_index == i:
                        cash += proceeds
                    else:
                        pending.append((cash_index, float(proceeds)))
                    lot.units -= units
                    remaining -= units
                    trades.append({"date": str(day.date()), "signal_date": str(signal_day.date()), "code": code,
                                   "side": "sell", "units": units, "gross": gross, "fee": gross - proceeds,
                                   "confirmation_date": str(lot.confirmed.date()), "cash_session_index": cash_index})
                lots = [x for x in lots if x.units > 1e-10]
            planned = {c: sum(x.units for x in lots if x.code == c) * float(known[c])
                       for c in prices.columns if pd.notna(known[c])}
            for code, weight in weights.items():
                rules = fund_rules(cfg, code)
                if not allowed(code, "buy"):
                    continue
                price = float(prices.iloc[i][code])
                current = planned.get(code, 0)
                if any(x.units > 0 and x.code != code and metadata[x.code]['group'] == metadata[code]['group'] for x in lots):
                    continue
                sector = metadata[code]["sector"]
                sector_used = sum(v for c, v in planned.items() if metadata[c]["sector"] == sector)
                budget = min(cash, max(0, total * weight - current),
                             max(0, total * cfg["rules"]["max_exposure"] - sum(planned.values())),
                             max(0, total * cfg["rules"]["max_sector_weight"] - sector_used))
                if status_history is not None:
                    history = status_history[(status_history.code == code) & (status_history.date <= day)].sort_values("date")
                    cap = history.iloc[-1].get("purchase_limit", np.nan)
                    if pd.notna(cap):
                        budget = min(budget, float(cap))
                if budget < rules["min_order"]:
                    continue
                fee = rules["buy_fee"]
                invested = budget / (1 + fee) * (1 - rules["slippage_bps"] / 10000)
                confirm_index = i + rules["confirmation_sessions"]
                # Confirmation outside sample cannot be redeemed during sample.
                confirm = dates[confirm_index] if confirm_index < len(dates) else day + pd.Timedelta(days=365)
                lots.append(Lot(code, invested / price, confirm))
                cash -= budget
                planned[code] = current + budget
                trades.append({"date": str(day.date()), "signal_date": str(signal_day.date()), "code": code,
                               "side": "buy", "gross": budget, "fee": budget - invested,
                               "confirmation_date": str(confirm.date())})
        records.append({"date": str(day.date()), "assets": float(nav_value(i)), "cash": float(cash),
                        "receivable": float(sum(x[1] for x in pending)), "regime": regime})
        if cash < -1e-7:
            raise AssertionError("回测现金透支")
    stats = metrics([x["assets"] for x in records], initial, cfg["model"]["risk_free_annual"])
    stats.update(fees=float(sum(x["fee"] for x in trades)), trades=len(trades))
    stats['turnover'] = float(sum(t['gross'] for t in trades) / max(np.mean([r['assets'] for r in records]),1))
    stats['fee_drag_initial'] = stats['fees'] / initial
    curve = pd.DataFrame(records)
    returns = curve.assets.pct_change().fillna(curve.assets.iloc[0]/initial-1)
    stats['regimes'] = {label: {'sessions':int((curve.regime==label).sum()),
                              'compound_return':float((1+returns[curve.regime==label]).prod()-1)}
                       for label in sorted(set(curve.regime))}
    return stats, curve, trades


def compare(frames, cfg, metadata, out="reports/backtest", start=None, end=None, status_history=None):
    prices = market_matrix(frames)
    if end:
        prices = prices.loc[:end]
    # Live selection only uses previous-day NAV, including for an evening report.
    prices = prices[prices.index.date < now_cn().date()]
    nval, ntest = cfg["model"]["validation_sessions"], cfg["model"]["test_sessions"]
    eligible = prices.index[LOOKBACK + cfg["rules"]["signal_lag_sessions"]:]
    if start:
        eligible = eligible[eligible >= pd.Timestamp(start)]
    if len(eligible) < nval + ntest + cfg["model"]["min_train_sessions"]:
        raise GuardError("历史数据不足以完成训练、验证、独立测试，请延长区间")
    train, validation, test = eligible[:-(nval + ntest)], eligible[-(nval + ntest):-ntest], eligible[-ntest:]
    periods = {"train": train, "validation": validation, "test": test}
    output = Path(out)
    output.mkdir(parents=True, exist_ok=True)
    results = {}
    # Selection is frozen before test metrics are consulted.
    for strategy in BASE_STRATEGIES:
        results[strategy] = {}
        for label in ("train", "validation"):
            days = periods[label]
            stat, _, _ = simulate(prices, cfg, metadata, strategy, days[0], days[-1], status_history)
            results[strategy][label] = stat
    candidates = [s for s in BASE_STRATEGIES if results[s]["validation"]["max_drawdown"] <= cfg["rules"]["validation_max_drawdown"]]
    selected = max(candidates, key=lambda s: results[s]["validation"]["total_return"])
    for strategy in BASE_STRATEGIES:
        stat, curve, trades = simulate(prices, cfg, metadata, strategy, test[0], test[-1], status_history)
        results[strategy]["test"] = stat
        curve.to_csv(output / (strategy + "-test-equity.csv"), index=False)
        write_json(output / (strategy + "-test-trades.json"), trades)
    result = {"created_at": now_cn().isoformat(), "config_hash": fingerprint(cfg),
              "selected": selected, "selection_rule": "验证集最大回撤达标后，按验证集净收益选择；独立测试集不参与选择",
              "data_end": str(prices.index[-1].date()), "codes": sorted(frames),
              "periods": {k: [str(v[0].date()), str(v[-1].date())] for k, v in periods.items()},
              "historical_status": "point_in_time" if status_history is not None else "assumed_open_not_competition_validated",
              "limitations": ["历史净值为当前供应商修订版，非逐日发布快照", "分红按红利再投的复权单位模拟，实际账户使用真实份额净值",
                              "当前基金池存在存续偏差，结果不代表比赛全池最优", "费率与申赎时滞需按比赛规则逐基金核验"],
              "results": results}
    write_json(output / "comparison.json", result)
    lines = ["# 真实净值策略比较", "", f"冻结选择：**{selected}**（按验证集选择，测试集仅用于检查泛化）", "",
             "以下为研究假设结果，不能视作已确认的比赛表现。", "",
             "|策略|验证收益|测试收益|测试最大回撤|测试夏普|测试期末资产|测试费用|", "|---|---:|---:|---:|---:|---:|---:|"]
    for strategy, parts in results.items():
        v, t = parts["validation"], parts["test"]
        lines.append(f"|{strategy}|{v['total_return']:.2%}|{t['total_return']:.2%}|{t['max_drawdown']:.2%}|{t['sharpe']:.2f}|{t['final_assets']:.2f}|{t['fees']:.2f}|")
    lines += ["", "区间：" + str(result["periods"]), "", *result["limitations"], "", "历史申赎状态：" + result["historical_status"]]
    (output / "comparison.md").write_text("\n".join(lines), encoding="utf-8")
    return result

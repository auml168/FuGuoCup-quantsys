from __future__ import annotations

from collections import OrderedDict
import numpy as np
import pandas as pd
from .universe import representative_codes

BASE_STRATEGIES = ('cash', 'equal_weight', 'momentum', 'relative_strength', 'sector_rotation', 'timing')
CHALLENGERS = ('dual_momentum', 'risk_budget', 'ensemble')
STRATEGIES = BASE_STRATEGIES + CHALLENGERS
LOOKBACK = 128
_FEATURE_CACHE = OrderedDict()


def features(prices):
    """Trailing observations only, without backfill. No future constituent eligibility."""
    complete = prices.iloc[-LOOKBACK:].notna().all()
    p = prices.loc[:, complete]
    if len(p) < LOOKBACK or p.empty:
        return pd.DataFrame()
    daily = p.pct_change(fill_method=None)
    out = pd.DataFrame(index=p.columns)
    for n in (20, 60, 126):
        out[f'mom{n}'] = p.iloc[-1] / p.iloc[-n-1] - 1
    out['vol60'] = daily.tail(60).std(ddof=1) * np.sqrt(252)
    out['vol20'] = daily.tail(20).std(ddof=1) * np.sqrt(252)
    out['trend'] = p.iloc[-1] / p.tail(120).mean() - 1
    out['drawdown60'] = p.iloc[-1] / p.tail(60).max() - 1
    out['risk_adjusted'] = out.mom60 / out.vol60.clip(lower=.01)
    out['multi_momentum'] = .25 * out.mom20 + .35 * out.mom60 + .40 * out.mom126
    out['score'] = 100 * (.30 * out.mom20.rank(pct=True) + .30 * out.mom60.rank(pct=True)
                           + .20 * out.risk_adjusted.rank(pct=True) + .20 * (-out.vol60).rank(pct=True))
    return out.replace([np.inf, -np.inf], np.nan).dropna()


def market_regime(f, metadata):
    if f.empty:
        return 'unknown', 0.0
    equities = f.loc[[c for c in f.index if metadata[c].get('asset_type', 'equity') != 'bond']]
    if equities.empty:
        return 'defensive_only', 0.0
    breadth = float(((equities.trend > 0) & (equities.mom60 > 0)).mean())
    return ('risk_on' if breadth >= .6 else 'mixed' if breadth >= .35 else 'risk_off'), breadth


def breadth_exposure(cfg, regime, default=0.5):
    return float(cfg.get('signal', {}).get('breadth_haircut', {}).get(regime, default))


def project_weights(raw, cfg, metadata, max_weight_by_code=None):
    """Reduce only; sector/group/fund caps and total exposure never require leverage."""
    result, sectors, groups = {}, {}, {}
    remaining = cfg['rules']['max_exposure']
    for c, value in sorted(raw.items(), key=lambda x: (-x[1], x[0])):
        sector, group = metadata[c]['sector'], metadata[c]['group']
        # One tradable share class per underlying, including equal-weight and ensemble.
        if group in groups:
            continue
        cap = cfg['rules']['max_fund_weight']
        if max_weight_by_code and c in max_weight_by_code:
            cap = min(cap, float(max_weight_by_code[c]))
        w = min(max(0.0, value), cap, remaining,
                max(0, cfg['rules']['max_sector_weight'] - sectors.get(sector, 0)))
        if w > 0:
            result[c] = float(w)
            groups[group] = c
            sectors[sector] = sectors.get(sector, 0) + w
            remaining -= w
    return result


def score_universe(prices):
    """Score every fund with a usable trailing window, without share-class dedupe.

    Used only for the coverage CSV/JSON, so a model-unsupported but NAV-ready
    fund still gets an analysis score and cannot be silently omitted.
    """
    return features(prices)


def target_weights(prices, strategy, cfg, metadata, eligible=None, min_history=None,
                   max_weight_by_code=None):
    key = (prices.attrs.get('research_cache_key'), str(prices.index[-1]) if len(prices) else '',
           len(prices), None if eligible is None else hash(tuple(sorted(eligible))), min_history)
    cached = _FEATURE_CACHE.get(key) if key[0] else None
    if cached is None:
        representatives = representative_codes(prices, metadata, LOOKBACK,
                                               eligible=eligible, min_history=min_history)
        p = prices.loc[:, representatives].tail(LOOKBACK).copy()
        p.attrs = {}  # Eligibility already checked; avoid copying quality metadata into feature math.
        f = features(p)
        if key[0]:
            _FEATURE_CACHE[key] = (p, f)
            if len(_FEATURE_CACHE) > 512:
                _FEATURE_CACHE.popitem(last=False)
    else:
        p, f = cached
    if strategy == 'cash' or f.empty:
        return {}, f
    full_scores = f
    # Drawdown floor is a soft risk gate: filtered names cannot be selected,
    # but the full score table is still returned for the report.
    floor = cfg.get('signal', {}).get('drawdown_floor', -0.20)
    g = f if strategy == 'equal_weight' else f[f.drawdown60 > floor]
    exposure = cfg['rules']['max_exposure']
    if strategy == 'ensemble':
        raw = {}
        for model in ('momentum', 'dual_momentum', 'risk_budget'):
            w, _ = target_weights(prices, model, cfg, metadata, eligible=eligible,
                                  min_history=min_history, max_weight_by_code=max_weight_by_code)
            for c, value in w.items():
                raw[c] = raw.get(c, 0) + value / 3
        return project_weights(raw, cfg, metadata, max_weight_by_code), full_scores
    if strategy == 'equal_weight':
        selected = list(g.index)
    elif strategy == 'momentum':
        regime, _ = market_regime(f, metadata)
        exposure *= breadth_exposure(cfg, regime)
        selected = g[g.mom60 > 0].sort_values('mom60', ascending=False).index.tolist()
    elif strategy == 'relative_strength':
        regime, _ = market_regime(f, metadata)
        exposure *= breadth_exposure(cfg, regime)
        selected = g[g.mom20 > 0].sort_values('risk_adjusted', ascending=False).index.tolist()
    elif strategy == 'sector_rotation':
        regime, _ = market_regime(f, metadata)
        exposure *= breadth_exposure(cfg, regime)
        sectors = pd.Series({c: metadata[c]['sector'] for c in g.index})
        sector_score = g.mom20.groupby(sectors).mean().sort_values(ascending=False)
        top = set(sector_score[sector_score > 0].head(2).index)
        selected = g[(sectors.isin(top)) & (g.mom60 > 0)].sort_values('score', ascending=False).index.tolist()
    elif strategy == 'timing':
        # Preserve original model as a baseline; it already cuts exposure by
        # its own trend-breadth rule, so no second haircut is applied.
        breadth = ((g.trend > 0) & (g.mom60 > 0)).mean()
        exposure *= 1 if breadth >= .6 else .4 if breadth >= .35 else 0
        selected = g[(g.trend > 0) & (g.mom60 > 0)].sort_values('score', ascending=False).index.tolist()
    elif strategy in CHALLENGERS:
        regime, _ = market_regime(f, metadata)
        exposure *= breadth_exposure(cfg, regime)
        equities = [c for c in g.index if metadata[c].get('asset_type', 'equity') != 'bond']
        bonds = [c for c in g.index if metadata[c].get('asset_type') == 'bond']
        eligible_rows = g.loc[equities]
        eligible_rows = eligible_rows[(eligible_rows.multi_momentum > 0) & (eligible_rows.trend > 0)
                                      & (eligible_rows.drawdown60 > -.15)]
        selected = eligible_rows.sort_values('multi_momentum', ascending=False).index.tolist()
        if regime == 'risk_off' or not selected:
            defensive = g.loc[bonds]
            selected = defensive[defensive.mom60 > 0].sort_values('risk_adjusted', ascending=False).index.tolist()
    else:
        raise ValueError('未知策略 ' + strategy)
    if strategy != 'equal_weight':
        selected = selected[:cfg['model']['top_k']]
    if strategy == 'risk_budget' and selected:
        # Greedy correlation screen uses only the trailing window. The cap is
        # a frozen research parameter; read it instead of silently hardcoding.
        correlation_cap = float(cfg.get('research', {}).get('correlation_cap', .85))
        corr = p[selected].pct_change(fill_method=None).tail(60).corr()
        diversified = []
        for c in selected:
            if all(pd.notna(corr.loc[c, d]) and corr.loc[c, d] < correlation_cap for d in diversified):
                diversified.append(c)
        selected = diversified
        inv = 1 / f.loc[selected].vol60.clip(lower=.04)
        raw = (inv / inv.sum() * exposure).to_dict()
        covariance = p[selected].pct_change(fill_method=None).tail(60).cov() * 252
        vector = pd.Series(raw).reindex(selected).to_numpy()
        predicted_vol = float(np.sqrt(max(0, vector @ covariance.to_numpy() @ vector)))
        scale = min(1.0, cfg.get('research', {}).get('target_vol', .12) / max(predicted_vol, .001))
        raw = {c: w * scale for c, w in raw.items()}
    else:
        raw = {c: exposure / len(selected) for c in selected} if selected and exposure > 0 else {}
    return project_weights(raw, cfg, metadata, max_weight_by_code), full_scores

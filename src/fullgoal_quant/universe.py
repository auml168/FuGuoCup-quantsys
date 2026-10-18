"""Product metadata inferred without using performance; sales catalogue is NOT competition approval."""
from __future__ import annotations

import re
from .common import GuardError


def classify(name, category):
    text = name + category
    # Three independent questions: is this a Fullgoal product, is it sold
    # off-exchange (OTC), and can the current model price/execute it? Keeping
    # them separate lets the coverage report list every OTC fund even when the
    # model does not support its mechanics.
    identity_verified = name.startswith("富国")

    otc_reasons = []
    if not identity_verified:
        otc_reasons.append("非富国名称；公司页通用链接不构成管理人证明")
    if "ETF" in name and "联接" not in name:
        otc_reasons.append("非联接ETF，不是本系统支持的场外品种")
    if "场内" in category:
        otc_reasons.append("场内类别")
    if "交易型" in name:
        otc_reasons.append("场内交易型基金，不是场外申购品种")
    if "REIT" in text:
        otc_reasons.append("REIT暂不支持")
    otc = not otc_reasons

    model_reasons = []
    for word, reason in [("QDII", "跨境/QDII披露及结算暂不支持"), ("FOF", "FOF暂不支持"),
                         ("货币", "货币基金需万份收益模型"), ("后端", "后端收费暂不支持"),
                         ("持有", "锁定持有期暂不支持"), ("定开", "定开产品暂不支持"),
                         ("定期开放", "定期开放暂不支持"), ("封闭", "封闭产品暂不支持"),
                         ("商品", "商品产品需专门模型"), ("黄金", "黄金/商品产品需专门模型"),
                         ("上海金", "黄金/商品产品需专门模型")]:
        if word in text:
            model_reasons.append(reason)
    if re.search(r'(一年期|二年期|两年期|三年期|[0-9]+个月|[0-9]+天|滚动|分级)', name):
        model_reasons.append('期限/滚动/分级产品须另行核验开放与结算机制')
    # Eastmoney uses "指数型-固收" for bond index funds; treating only the
    # literal "债券" as fixed income misclassified them as equity.
    fixed_income = "债券" in category or "固收" in category
    asset = "bond" if fixed_income and not any(x in text for x in ("转债", "混合一级", "混合二级", "偏债")) else "equity"
    if "转债" in text or "混合一级" in text or "混合二级" in text or "偏债" in category:
        asset = "hybrid_bond"
    if not any(x in category for x in ("股票", "混合", "债券", "固收", "指数")):
        model_reasons.append("类别未知或尚无对应收益模型")
    model_supported = otc and not model_reasons

    share = re.search(r"([A-Z](?:/[A-Z])*)$", name)
    group = re.sub(r"([A-Z](?:/[A-Z])*)$", "", name).strip()
    sector = {"bond": "债券", "hybrid_bond": "混合债"}.get(asset, "主动权益")
    for words, label in [(("新能源", "光伏", "电池"), "新能源"), (("军工", "国防"), "军工"),
                         (("医药", "医疗", "生物"), "医药"), (("银行",), "银行"),
                         (("证券",), "证券"), (("地产",), "地产"), (("消费", "食品", "白酒"), "消费"),
                         (("红利",), "红利"), (("半导体", "芯片", "计算机", "科技", "人工智能"), "科技"),
                         (("沪深300", "中证500", "中证1000", "创业板", "上证"), "宽基")]:
        if any(word in name for word in words):
            sector = label
            break
    return {"identity_verified": identity_verified, "otc": otc,
            "model_supported": model_supported, "otc_candidate": model_supported,
            "otc_exclusion_reasons": sorted(set(otc_reasons)),
            "model_unsupported_reasons": sorted(set(model_reasons)),
            "unsupported_reasons": sorted(set(otc_reasons + model_reasons)),
            "asset_type": asset, "group": group, "share_class": share[1] if share else None,
            "group_source": "name_heuristic", "sector": sector, "sector_source": "name_heuristic",
            "competition_approved": False}


def metadata_for(cfg, snapshot):
    legacy = {x['code']: x for x in cfg.get('research_funds', [])}
    universe = cfg.get('universe', {})
    min_history = universe.get('min_history', 252)
    min_score = universe.get('min_score_history', 128)
    result = {}
    for c,f in snapshot['funds'].items():
        base = {**f, **classify(f['name'],f.get('category',''))}
        if 'group' not in f:
            base.update(legacy.get(c,{}))  # Legacy fixtures/baselines only.
        base.update(cfg.get('product_overrides',{}).get(c,{}))
        # Live scoring needs only the feature window; full backtests pass an
        # explicit 252-session minimum when they call target_weights().
        base['required_history'] = min_score if 'history_rows' in f else 128
        base['backtest_required_history'] = min_history
        result[c]=base
    return result


def active_codes(cfg, snapshot, competition=False):
    """Model-supported funds with a full backtest history (research/backtest)."""
    if competition:
        codes = cfg["competition"]["allowed_codes"]
        if not codes or set(codes) - set(snapshot["funds"]):
            raise GuardError("比赛白名单为空或本次数据不完整")
        return codes
    return [c for c, f in snapshot["funds"].items() if f.get("research_ready", True)
            and (classify(f['name'],f['category'])['model_supported'] if 'category' in f else True)]


def analysis_codes(cfg, snapshot):
    """Every OTC fund with enough history to score, regardless of model support.

    This is the coverage/analysis universe. QDII/FOF/lock-up/open-period funds
    are included when their NAV series is usable; they remain ineligible for
    buy weights via the report's tradeable filter.
    """
    min_score = cfg.get('universe', {}).get('min_score_history', 128)
    codes = []
    for c, f in snapshot['funds'].items():
        if 'category' in f and not classify(f['name'], f['category'])['otc']:
            continue
        if f.get('collection_status') in ('error', 'not_otc', 'not_requested', 'model_unsupported'):
            continue
        if f.get('analysis_ready') is False:
            continue
        rows = f.get('history_rows')
        if rows is not None and rows < min_score:
            continue
        codes.append(c)
    return sorted(codes)


def representative_codes(prices, metadata, lookback, eligible=None, min_history=None):
    # Eligibility is computed from the historical prefix, never today's oldest survivor.
    ready = prices.columns[prices.tail(lookback).notna().all()]
    if eligible is not None:
        from pandas import Index
        ready = ready.intersection(Index(eligible))
    groups = {}
    counts=prices.notna().sum()
    requirements={c:(min_history if min_history is not None else metadata[c].get('required_history',lookback))
                  for c in ready}
    complete_tail=prices.tail(max(requirements.values(),default=lookback)).notna().all()
    for code in sorted(ready):
        required=requirements[code]
        if counts[code]<required:
            continue
        known_breaks = [event for event in prices.attrs.get('quality_breaks', {}).get(code, []) if event <= prices.index[-1]]
        if known_breaks:
            if complete_tail[code]:
                if max(known_breaks)>=prices.index[-required]:
                    continue
            elif prices.loc[prices.index > max(known_breaks), code].notna().sum()<required:
                continue
        groups.setdefault(metadata[code]["group"], code)
    return list(groups.values())

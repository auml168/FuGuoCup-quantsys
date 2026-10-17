"""Product metadata inferred without using performance; sales catalogue is NOT competition approval."""
from __future__ import annotations

import re
from .common import GuardError


def classify(name, category):
    text = name + category
    reasons = []
    if not name.startswith("富国"):
        reasons.append("非富国名称；公司页通用链接不构成管理人证明")
    for word, reason in [("QDII", "跨境/QDII披露及结算暂不支持"), ("FOF", "FOF暂不支持"),
                         ("货币", "货币基金需万份收益模型"), ("后端", "后端收费暂不支持"),
                         ("持有", "锁定持有期暂不支持"), ("定开", "定开产品暂不支持"),
                         ("定期开放", "定期开放暂不支持"), ("封闭", "封闭产品暂不支持"),
                         ("商品", "商品产品需专门模型"), ("REIT", "REIT暂不支持")]:
        if word in text:
            reasons.append(reason)
    if "ETF" in name and "联接" not in name:
        reasons.append("非联接ETF，不是本系统支持的场外品种")
    if "场内" in category:
        reasons.append("场内类别")
    if re.search(r'(一年期|二年期|两年期|三年期|[0-9]+个月|[0-9]+天|滚动|分级)', name):
        reasons.append('期限/滚动/分级产品须另行核验开放与结算机制')
    asset = "bond" if "债券" in category and not any(x in text for x in ("转债", "混合一级", "混合二级")) else "equity"
    if "转债" in text or "混合一级" in text or "混合二级" in text:
        asset = "hybrid_bond"
    if not any(x in category for x in ("股票", "混合", "债券", "指数")):
        reasons.append("类别未知或尚无对应收益模型")
    share = re.search(r"([A-Z](?:/[A-Z])*)$", name)
    group = re.sub(r"([A-Z](?:/[A-Z])*)$", "", name).strip()
    sector = "债券" if asset == "bond" else "主动权益"
    for words, label in [(("新能源", "光伏", "电池"), "新能源"), (("军工", "国防"), "军工"),
                         (("医药", "医疗", "生物"), "医药"), (("银行",), "银行"),
                         (("证券",), "证券"), (("地产",), "地产"), (("消费", "食品", "白酒"), "消费"),
                         (("红利",), "红利"), (("半导体", "芯片", "计算机", "科技", "人工智能"), "科技"),
                         (("沪深300", "中证500", "中证1000", "创业板", "上证"), "宽基")]:
        if any(word in name for word in words):
            sector = label
            break
    return {"otc_candidate": not reasons, "unsupported_reasons": sorted(set(reasons)), "asset_type": asset,
            "group": group, "share_class": share[1] if share else None, "group_source": "name_heuristic",
            "sector": sector, "sector_source": "name_heuristic", "competition_approved": False}


def metadata_for(cfg, snapshot):
    legacy = {x['code']: x for x in cfg.get('research_funds', [])}
    result = {}
    for c,f in snapshot['funds'].items():
        base = {**f, **classify(f['name'],f.get('category',''))}
        if 'group' not in f:
            base.update(legacy.get(c,{}))  # Legacy fixtures/baselines only.
        base.update(cfg.get('product_overrides',{}).get(c,{}))
        base['required_history']=cfg.get('universe',{}).get('min_history',252) if 'history_rows' in f else 128
        result[c]=base
    return result


def active_codes(cfg, snapshot, competition=False):
    if competition:
        codes = cfg["competition"]["allowed_codes"]
        if not codes or set(codes) - set(snapshot["funds"]):
            raise GuardError("比赛白名单为空或本次数据不完整")
        return codes
    return [c for c, f in snapshot["funds"].items() if f.get("research_ready", True)
            and (classify(f['name'],f['category'])['otc_candidate'] if 'category' in f else True)]


def representative_codes(prices, metadata, lookback):
    # Eligibility is computed from the historical prefix, never today's oldest survivor.
    ready = prices.columns[prices.tail(lookback).notna().all()]
    groups = {}
    counts=prices.notna().sum()
    requirements={c:metadata[c].get('required_history',lookback) for c in ready}
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

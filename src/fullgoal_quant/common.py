from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")


class GuardError(ValueError):
    """Expected safety/data/configuration failure, safe to display."""


def now_cn():
    return datetime.now(TZ)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(tmp, path)


def fingerprint(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def amount(value, label="金额"):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GuardError(f"{label}须为数值，不能使用字符串或布尔值")
    v = float(value)
    if not math.isfinite(v) or v < 0:
        raise GuardError(f"{label}须为非负有限数值")
    return v


def load_config(path="config/settings.json"):
    cfg = read_json(path)
    competition_schedule(cfg["competition"], now_cn().date())
    rules = cfg["rules"]
    for key in ("max_fund_weight", "max_sector_weight", "max_exposure", "validation_max_drawdown"):
        if not 0 < rules[key] <= 1:
            raise GuardError(f"rules.{key} 须在 (0,1] 内")
    for key in ("confirmation_sessions", "redemption_sessions", "signal_lag_sessions", "min_hold_days"):
        if not isinstance(rules[key], int) or rules[key] < 0:
            raise GuardError(f"rules.{key} 须为非负整数")
    if rules["signal_lag_sessions"] < 2:
        raise GuardError("回测至少滞后两个净值交易日，避免提前使用未公布净值")
    if rules["rebalance_sessions"] < 1 or cfg["model"]["top_k"] < 1:
        raise GuardError("调仓间隔和持有数量须大于0")
    mode = rules.get("rebalance_mode", "weekly")
    if mode not in ("weekly", "sessions"):
        raise GuardError("rules.rebalance_mode 须为 weekly 或 sessions")
    if mode == "weekly" and rules["rebalance_sessions"] != 5:
        raise GuardError("weekly 再平衡要求 rebalance_sessions=5，避免记录与执行不一致")
    for key in ("validation_sessions", "test_sessions", "min_train_sessions"):
        if not isinstance(cfg["model"].get(key), int) or cfg["model"][key] <= 0:
            raise GuardError(f"model.{key} 须为正整数")
    research = cfg.get("research", {})
    for key in ("fold_sessions", "holdout_sessions", "min_train_sessions"):
        if not isinstance(research.get(key), int) or research[key] <= 0:
            raise GuardError(f"research.{key} 须为正整数")
    if not isinstance(research.get("min_folds"), int) or research["min_folds"] < 2:
        raise GuardError("research.min_folds 须为至少2的整数，单窗口无法估计超额分布")
    for rule in [rules] + list(cfg.get("fund_rules", {}).values()):
        for key in ("confirmation_sessions", "redemption_sessions", "min_hold_days"):
            if key in rule and (not isinstance(rule[key], int) or rule[key] < 0):
                raise GuardError(f"{key} 须为非负整数")
        if rule.get("redemption_sessions", 1) < 1:
            raise GuardError("未知净值成交的赎回款不可假设同日用于申购；redemption_sessions 至少为1")
        if "min_order" in rule and amount(rule["min_order"]) <= 0:
            raise GuardError("min_order 须大于0")
        if "slippage_bps" in rule and not 0 <= rule["slippage_bps"] < 1000:
            raise GuardError("slippage_bps 须在 [0,1000) 内")
        if "buy_fee" in rule and not 0 <= rule["buy_fee"] < 1:
            raise GuardError("申购费率无效")
        if "redemption_fee_tiers" in rule:
            last = 0
            for upper, fee in rule["redemption_fee_tiers"]:
                if upper <= last or not 0 <= fee < 1:
                    raise GuardError("赎回费阶梯须递增且费率有效")
                last = upper
    codes = [f["code"] for f in cfg["research_funds"]]
    if len(codes) != len(set(codes)) or any(len(x) != 6 or not x.isdigit() for x in codes):
        raise GuardError("研究池基金代码须为不重复的六位字符串")
    return cfg


def competition_schedule(competition, today):
    """Calendar-day countdowns; dates include the cutoff day, not a guessed cutoff time."""
    try:
        start = date.fromisoformat(competition["start_date"])
        midpoint = date.fromisoformat(competition["midpoint_date"]) if competition.get("midpoint_date") else None
        end = date.fromisoformat(competition["end_date"]) if competition.get("end_date") else None
    except (TypeError, ValueError):
        raise GuardError("赛程日期须为有效的 YYYY-MM-DD") from None
    if (end and end < start) or (midpoint and midpoint < start) or (midpoint and end and midpoint > end):
        raise GuardError("赛程须满足开始日 ≤ 半程截止日 ≤ 结束日")
    phase = "赛前" if today < start else ("已结束" if end and today > end else
            ("半程赛" if midpoint and today <= midpoint else "后半程" if midpoint else "比赛中"))
    return {"start_date": str(start), "midpoint_date": str(midpoint) if midpoint else None,
            "end_date": str(end) if end else None, "phase": phase,
            "days_to_start": (start - today).days,
            "days_to_midpoint": (midpoint - today).days if midpoint else None,
            "days_to_end": (end - today).days if end else None}


def fund_rules(cfg, code):
    return {**cfg["rules"], **cfg.get("fund_rules", {}).get(code, {})}


def redemption_fee(rules, age):
    for upper, rate in rules["redemption_fee_tiers"]:
        if age < upper:
            return rate
    return 0.0


def competition_errors(cfg):
    c = cfg["competition"]
    errors = []
    if not c.get("verified") or not c.get("rules_source"):
        errors.append("比赛规则尚未核验")
    if not c.get("allowed_codes") or not c.get("universe_source"):
        errors.append("比赛可交易白名单尚未核验")
    if not c.get("end_date"):
        errors.append("比赛截止日尚未确认")
    if c.get('allowed_codes') and (len(c['allowed_codes']) != len(set(c['allowed_codes'])) or any(not isinstance(x,str) or len(x)!=6 or not x.isdigit() for x in c['allowed_codes'])):
        errors.append('比赛白名单代码无效或重复')
    if c.get('verified'):
        required = {'buy_fee','redemption_fee_tiers','confirmation_sessions','redemption_sessions','min_hold_days','min_order','source','verified'}
        for code in c.get('allowed_codes', []):
            r = cfg.get('fund_rules', {}).get(code,{})
            if required - set(r) or not r.get('verified') or not r.get('source'):
                errors.append(f'{code} 缺少逐基金实际费率/申赎字段；不能继承研究默认值放行')
    return errors

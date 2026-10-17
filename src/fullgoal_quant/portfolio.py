from __future__ import annotations

from datetime import date, datetime

from .common import GuardError, amount, fund_rules, read_json, redemption_fee, write_json


def validate_account(account):
    date.fromisoformat(account["as_of"])
    amount(account["cash"], "可用现金")
    if 'reconciled_at' in account:
        stamp = datetime.fromisoformat(account['reconciled_at'])
        if stamp.tzinfo is None:
            raise GuardError('reconciled_at 须包含时区')
    for lot in account["lots"]:
        if not isinstance(lot["code"], str) or len(lot["code"]) != 6 or not lot["code"].isdigit():
            raise GuardError("持仓代码须为六位字符串")
        if amount(lot["units"], "份额") == 0:
            raise GuardError("持仓份额须大于0")
        date.fromisoformat(lot["confirmed_on"])
    for pending in account.get("pending", []):
        amount(pending["amount"], "在途资产")
        date.fromisoformat(pending["settle_on"])
        if pending["type"] not in ("buy", "sell"):
            raise GuardError("在途 type 须为 buy 或 sell")
    return account


def import_account(source, destination="config/account.json"):
    account = validate_account(read_json(source))
    from pathlib import Path
    if Path(destination).exists():
        write_json(str(destination) + ".backup", read_json(destination))
    write_json(destination, account)
    return account


def propose(account, weights, navs, snapshot, cfg, today, active_codes, metadata=None):
    """Use actual shares and latest unit NAV; redemption cash is not immediately available."""
    validate_account(account)
    if any(not isinstance(v,(int,float)) or not 0 <= v <= cfg['rules']['max_fund_weight'] for v in weights.values()) or sum(weights.values()) > cfg['rules']['max_exposure'] + 1e-9:
        raise GuardError('目标仓位非法或超出风险上限')
    for c, nav in navs.items():
        if amount(nav, '单位净值') <= 0:
            raise GuardError('单位净值须大于0')
    for info in snapshot.values():
        if not isinstance(info['buy_open'],bool) or not isinstance(info['sell_open'],bool):
            raise GuardError('申赎状态须为明确布尔值')
    errors = []
    asof = date.fromisoformat(account["as_of"])
    if (today - asof).days > cfg["account_max_age_days"] or today < asof:
        errors.append("实际账户核对日期过期或晚于当前日期，请更新持仓")
    if account.get("pending"):
        errors.append("账户存在在途申赎，请与平台核对并更新后再生成操作金额")
    held = {}
    for lot in account["lots"]:
        code = lot["code"]
        if code not in active_codes or code not in navs:
            errors.append(f"持仓 {code} 不在已核验白名单/数据池，请人工处理")
        else:
            held[code] = held.get(code, 0) + lot["units"] * navs[code]
        if date.fromisoformat(lot["confirmed_on"]) > today:
            errors.append(f"{code} 份额尚未确认，应移入 pending")
    assets = float(account["cash"]) + sum(held.values()) + sum(p["amount"] for p in account.get("pending", []))
    if assets <= 0:
        errors.append("账户资产须大于0")
    if errors:
        return {"assets": assets, "cash": account["cash"], "orders": [], "errors": errors}
    orders, cash = [], float(account["cash"])
    # A fund over target is reduced only if its eligible actual lots can be redeemed.
    for code in sorted(set(held) | set(weights)):
        rule = fund_rules(cfg, code)
        target = assets * weights.get(code, 0)
        difference = target - held.get(code, 0)
        if difference > -rule["min_order"]:
            continue
        eligible = [x for x in account["lots"] if x["code"] == code and (today - date.fromisoformat(x["confirmed_on"])).days >= rule["min_hold_days"]]
        units = min(-difference / navs[code], sum(x["units"] for x in eligible))
        units = int(units * 100) / 100  # never round above eligible shares
        gross = units * navs[code]
        fee, remainder = 0.0, units
        for lot in sorted(eligible, key=lambda x: x["confirmed_on"]):
            take = min(remainder, lot["units"])
            age = (today - date.fromisoformat(lot["confirmed_on"])).days
            fee += take * navs[code] * redemption_fee(rule, age)
            remainder -= take
        reason = "降至目标仓位；赎回款到账前不用于申购"
        allowed = snapshot[code]["sell_open"] and gross >= rule["min_order"]
        orders.append({"code": code, "action": "卖出" if allowed else "暂缓卖出", "amount": round(gross, 2) if allowed else 0,
                       "units": units if allowed else 0, "fee_estimate": round(fee, 2), "target_weight": weights.get(code, 0),
                       "reason": reason if allowed else "暂停赎回、未满最短持有期或不足最小金额"})
    # Current exposure includes unsold / locked holdings. Do not add risk on top of them.
    residual = dict(held)  # Planned redemptions do not free risk capacity until reconciled.
    metadata = metadata or {f['code']:f for f in cfg['research_funds']}
    sector_of = {c: f['sector'] for c,f in metadata.items()}
    groups = {metadata[c]['group'] for c,v in held.items() if v > 0}
    for code, weight in sorted(weights.items(), key=lambda x: x[1], reverse=True):
        rule = fund_rules(cfg, code)
        difference = assets * weight - held.get(code, 0)
        if difference < rule["min_order"]:
            continue
        cap = snapshot[code].get("purchase_limit")
        sector = sector_of[code]
        sector_used = sum(v for c, v in residual.items() if sector_of[c] == sector)
        budget = min(difference, cash, cap if cap is not None else float("inf"),
                     max(0, assets * cfg["rules"]["max_exposure"] - sum(residual.values())),
                     max(0, assets * cfg["rules"]["max_sector_weight"] - sector_used))
        allowed = snapshot[code]["buy_open"] and budget >= rule["min_order"]
        if metadata[code]['group'] in groups and not held.get(code):
            allowed = False  # Do not switch into a second A/C class while its sibling is held.
        paid = int(budget * 100) / 100 if allowed else 0
        orders.append({"code": code, "action": "买入" if allowed else "暂缓买入", "amount": paid,
                       "units": None, "fee_estimate": round(paid - paid / (1 + rule["buy_fee"]), 2),
                       "target_weight": weight,
                       "reason": "按策略目标增配，使用账户现有可用现金" if allowed else "暂停申购、限额、现金/风险额度不足"})
        cash -= paid
        residual[code] = residual.get(code, 0) + paid
        if paid:
            groups.add(metadata[code]['group'])
    return {"assets": assets, "cash": account["cash"], "cash_after_buys": cash, "orders": orders, "errors": []}

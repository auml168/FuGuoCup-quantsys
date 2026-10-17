"""Delivery reservations. Cloud must durably upload reservations BEFORE sendMessage."""
from __future__ import annotations
from pathlib import Path
from .common import GuardError, fingerprint, now_cn, read_json, write_json


def account_hash(account):
    return fingerprint({k:account.get(k,[]) for k in ('cash','lots','pending')})


def state_at(path='runtime/notification-state.json'):
    return read_json(path) if Path(path).exists() else {'version':1,'deliveries':{},'last_actionable':None}


def reserve(report, account, path='runtime/notification-state.json', owner='local'):
    state=state_at(path)
    key=report['report_id']
    if key in state['deliveries']:
        return False
    if report.get('actionable'):
        previous=state.get('last_actionable')
        if previous and previous['account_hash']==account_hash(account) and account.get('acknowledged_plan_id')!=previous['id']:
            raise GuardError('上一份可执行建议尚未核对/确认，不再重复生成交易指令')
        state['last_actionable']={'id':key,'account_hash':account_hash(account)}
    state['deliveries'][key]={'status':'reserved','owner':str(owner),'at':now_cn().isoformat()}
    # Bound public state size; last actionable reconciliation is retained independently.
    state['deliveries']=dict(sorted(state['deliveries'].items())[-400:])
    write_json(path,state)
    return True


def complete(report_id,path='runtime/notification-state.json'):
    state=state_at(path)
    state['deliveries'][report_id]['status']='sent'
    write_json(path,state)

"""Small, public delivery status; never store account data or exception text."""
import os
from pathlib import Path
from .common import TZ, now_cn, write_json


def production():
    return os.environ.get('FQ_EVENT') == 'schedule' or (
        os.environ.get('FQ_EVENT') == 'workflow_dispatch' and os.environ.get('FQ_MODE') == 'scheduled')


def record(status, **fields):
    at=now_cn().astimezone(TZ)
    slot=fields.get('slot', 'afternoon' if production() else 'manual')
    result={'status':status, 'slot':slot,
            'report_id':fields.get('report_id', f'{at.date()}-{slot}'),
            'event':os.environ.get('FQ_EVENT','local'),
            'run_id':os.environ.get('GITHUB_RUN_ID','local'),
            'at':at.isoformat(), 'detail':fields.get('detail','')}
    write_json('runtime/run-status.json',result)
    line=f"FQ_STATUS={status} slot={slot} report_id={result['report_id']} run_id={result['run_id']} {result['detail']}".strip()
    print(line)
    summary=os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with Path(summary).open('a',encoding='utf-8') as f:
            f.write('- '+line+'\n')
    return result

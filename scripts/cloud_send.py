"""Run only AFTER the reserved-state artifact upload step succeeds."""
import os
from pathlib import Path
from datetime import datetime
from fullgoal_quant.common import read_json, now_cn
from fullgoal_quant.cli import slot_guard
from fullgoal_quant.telegram import send_report
from fullgoal_quant.notifications import state_at,complete


def send():
    if not Path('runtime/outbox.json').exists():
        print('No new eligible report to send.');return
    report=read_json('runtime/outbox.json')
    if datetime.fromisoformat(report['created_at']).date()!=now_cn().date() or not slot_guard(report['slot']):
        raise SystemExit('Expired report suppressed; reservation retained.')
    item=state_at()['deliveries'].get(report['report_id'],{})
    if item.get('status')!='reserved' or item.get('owner')!=os.environ.get('GITHUB_RUN_ID','local'):
        raise SystemExit('Delivery reservation does not belong to this run.')
    send_report(report['text'])
    complete(report['report_id'])
    print('Telegram delivery completed.')


if __name__=='__main__':send()

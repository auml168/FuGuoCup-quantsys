"""Run only AFTER the reserved-state artifact upload step succeeds."""
import os
import sys
from pathlib import Path
from datetime import datetime
from fullgoal_quant.common import GuardError, TZ, read_json, now_cn
from fullgoal_quant.cli import slot_guard
from fullgoal_quant.telegram import send_report
from fullgoal_quant.notifications import state_at, complete, fail
from fullgoal_quant.runstatus import record


def send():
    if not Path('runtime/outbox.json').exists():
        try:
            status=read_json('runtime/run-status.json').get('status')
        except (OSError, ValueError, TypeError):
            status=None
        if status in ('skipped_non_session','skipped_already_sent','skipped_stale_trigger','preview'):
            return 0
        print(f'未生成发送预约（status={status}）')
        return 1
    report=read_json('runtime/outbox.json')
    fields={'slot':report['slot'],'report_id':report['report_id']}
    if datetime.fromisoformat(report['created_at']).astimezone(TZ).date()!=now_cn().date() or not slot_guard(report['slot']):
        record('failed',**fields,detail='报告已过期，保留预约禁止重发')
        raise SystemExit('报告已过期，保留预约禁止重发')
    item=state_at()['deliveries'].get(report['report_id'],{})
    if item.get('status')!='reserved' or item.get('owner')!=os.environ.get('GITHUB_RUN_ID','local'):
        record('failed',**fields,detail='发送预约不属于本次运行')
        raise SystemExit('发送预约不属于本次运行')
    try:
        if report.get('telegram_text'):
            send_report(report['telegram_text'], parse_mode='HTML')
        else:
            send_report(report['text'])
    except GuardError as exc:
        reason='http' if 'HTTP' in str(exc) else 'network' if '网络' in str(exc) else 'other'
        fail(report['report_id'],reason)
        record('send_failed',**fields,detail=reason)
        return 1
    complete(report['report_id'],report=report)
    record('test_sent' if report.get('test') else 'sent',**fields)
    print('Telegram 发送完成。')
    return 0


if __name__=='__main__':sys.exit(send())

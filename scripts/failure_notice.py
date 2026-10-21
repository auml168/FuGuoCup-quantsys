"""No dependencies beyond the package's stdlib Telegram component."""
from fullgoal_quant.telegram import send_report
from fullgoal_quant.common import GuardError, read_json
from fullgoal_quant.runstatus import production
from pathlib import Path


def already_delivered():
    """Do not claim 'no advice' when the reserved report was already sent."""
    try:
        outbox = read_json('runtime/outbox.json' if Path('runtime/outbox.json').exists() else 'runtime/run-status.json')
        state = read_json('runtime/notification-state.json')
        item = state.get('deliveries', {}).get(outbox.get('report_id', ''), {})
        return item.get('status') == 'sent'
    except (OSError, ValueError, KeyError, TypeError):
        return False


def notice():
    if already_delivered():
        print('报告已发送，抑制误导性失败通知。')
        return 0
    try:
        result=read_json('runtime/run-status.json')
        status=result['status']
        detail=result.get('detail','')
    except (OSError, ValueError, KeyError, TypeError):
        status,detail='failed','运行状态缺失'
    if status == 'skipped_stale_trigger':
        print('过期调度事件，不发送失败通知。')
        return 0
    prefix='今日14:20信号未送达' if production() else '手动报告任务失败'
    unresolved='消息可能已部分送达，请人工核对 Telegram，勿重复操作。' if status in ('blocked_unresolved','send_failed') else ''
    try:
        send_report(f'{prefix}：{status}'+(f'（{detail}）' if detail else '')+
                    '。本时段无有效交易建议，勿沿用旧金额。'+unresolved)
    except GuardError:
        print('故障通知未送达；请检查 GitHub Actions 和 Telegram 配置。')
        return 2
    return 0


if __name__=='__main__':
    raise SystemExit(notice())

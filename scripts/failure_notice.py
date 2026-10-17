"""No dependencies beyond the package's stdlib Telegram component."""
from fullgoal_quant.telegram import send_report
from fullgoal_quant.common import GuardError, read_json


def already_delivered():
    """Do not claim 'no advice' when the reserved report was already sent."""
    try:
        outbox = read_json('runtime/outbox.json')
        state = read_json('runtime/notification-state.json')
        item = state.get('deliveries', {}).get(outbox.get('report_id', ''), {})
        return item.get('status') == 'sent'
    except (OSError, ValueError, KeyError, TypeError):
        return False


if already_delivered():
    print('Report already delivered; suppressing misleading failure notice.')
    raise SystemExit(0)
try:
    send_report('富国杯分析任务失败，本时段没有可执行交易建议。请查看 GitHub Actions 状态，勿沿用旧金额。')
except GuardError:
    print('故障通知未送达；请检查 GitHub Actions 和 Telegram 配置。')
    raise SystemExit(2) from None

"""No dependencies beyond the package's stdlib Telegram component."""
from fullgoal_quant.telegram import send_report
from fullgoal_quant.common import GuardError

try:
    send_report('富国杯分析任务失败，本时段没有可执行交易建议。请查看 GitHub Actions 状态，勿沿用旧金额。')
except GuardError:
    print('故障通知未送达；请检查 GitHub Actions 和 Telegram 配置。')
    raise SystemExit(2) from None
